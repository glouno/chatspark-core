"""Application API v1. Callers authorize access; the engine verifies evidence."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chatspark.generation.prompts import resolve_messages
from chatspark.profiles.loader import profile_digest
from chatspark.serve.contracts import SourceItem
from chatspark.storage.evidence import narrow_filters, snapshot_digest

from .capacity import AnswerCapacityV1


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServingSnapshotV1(StrictModel):
    schema_version: Literal["chatspark.serving-snapshot.v1"] = "chatspark.serving-snapshot.v1"
    build_id: str = Field(min_length=1, max_length=200)
    database: Path
    corpus_id: str = Field(min_length=1)
    chunk_set_id: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_collection: str | None = None
    embedding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AnswerRequestV1(StrictModel):
    question: str = Field(min_length=1, max_length=16000)
    filters: dict[str, object] = Field(default_factory=dict)
    top_k: int | None = Field(default=None, ge=1, le=100)

    @field_validator("filters")
    @classmethod
    def valid_filters(cls, value):
        return narrow_filters({}, value)


class _StatusData(StrictModel):
    phase: str = Field(min_length=1, max_length=64)


class _TokenData(StrictModel):
    delta: str = Field(max_length=65536)


class _SourcesData(StrictModel):
    sources: list[SourceItem] = Field(max_length=1000)


class _DoneData(_SourcesData):
    answer: str = Field(max_length=262144)
    corpus_scope: Literal["unified"] = "unified"
    usage: dict[str, object] | None = None
    gateway_attempt_id: str | None = Field(default=None, max_length=200)


class _ErrorData(StrictModel):
    code: Literal["capacity_busy", "generation_error", "snapshot_unavailable", "cancelled"]
    message: Literal["Chat request failed"] | None = None


class AnswerEventV1(StrictModel):
    schema_version: Literal["chatspark.answer-event.v1"] = "chatspark.answer-event.v1"
    event: Literal["status", "token", "sources", "done", "error"]
    data: dict[str, object]

    @model_validator(mode="after")
    def valid_payload(self):
        models = {
            "status": _StatusData,
            "token": _TokenData,
            "sources": _SourcesData,
            "done": _DoneData,
            "error": _ErrorData,
        }
        validated = models[self.event].model_validate(self.data)
        object.__setattr__(self, "data", validated.model_dump(exclude_unset=True))
        return self


class StreamCompletionV1(StrictModel):
    """Request-local closure evidence; unproved also covers cleanup in progress."""

    schema_version: Literal["chatspark.stream-completion.v1"] = "chatspark.stream-completion.v1"
    state: Literal["unproved", "succeeded", "failed"] = "unproved"


class AnswerStreamCloseError(RuntimeError):
    """Bounded public error; original failure is retained only as an in-memory cause."""

    def __init__(self):
        super().__init__("Application stream closure failed")


@runtime_checkable
class AnswerStreamV1(Protocol):
    """Optional additive capability; legacy AnswerRuntime iterators remain valid."""

    @property
    def close_completion(self) -> StreamCompletionV1: ...
    def __aiter__(self) -> AsyncIterator[AnswerEventV1]: ...
    async def __anext__(self) -> AnswerEventV1: ...
    async def aclose(self) -> None: ...


class AnswerRuntime(Protocol):
    snapshot: ServingSnapshotV1

    async def warmup(self) -> None: ...
    def readiness(self) -> Mapping[str, object]: ...
    def stream_answer(self, request: AnswerRequestV1) -> AsyncIterator[AnswerEventV1]: ...
    async def shutdown(self) -> None: ...


class AnswerModel(Protocol):
    """Immutable model binding. Stream handles own metadata and cancellation."""

    def stream_chat(self, messages, *, max_tokens: int): ...


def prepare_answer(payload, profile, retriever, *, resolver=resolve_messages):
    result = retriever.retrieve_result(
        payload.question, payload.top_k or profile.retrieval.final_k, payload.filters or {}
    )
    context = replace(
        retriever.context,
        filters=narrow_filters(retriever.context.filters, payload.filters or {}),
    )
    messages, metadata = resolver(payload.question, result.items, profile, context=context)
    sources = [
        SourceItem(
            chunk_id=c.chunk_id,
            doc_id=c.metadata["doc_id"],
            url=c.metadata["url"],
            title=c.metadata.get("title") or "Source",
            heading_path=c.metadata.get("heading_path") or [],
            retrieval_corpus_id=c.metadata["corpus_id"],
            selected_corpus_scope="unified",
        )
        for c in result.items
        if c.chunk_id in metadata["included_evidence_ids"]
    ]
    return messages, sources


def validate_citations(text, sources):
    """Return the completed answer's label table, empty when no evidence is cited."""
    available = {f"C{i}" for i in range(1, len(sources) + 1)}
    cited = set(re.findall(r"\[(C\d+)\]", text))
    if not cited <= available:
        raise ValueError("Unavailable citation")
    # Array positions define C1, C2, ... for streamed text. Compacting a partially
    # cited table would silently attach its existing markers to different sources.
    return sources if cited else []


class _StreamControl:
    def __init__(self, fail):
        self.stopped = Event()
        self.lock = Lock()
        self.provider = None
        self.cancel_started = False
        self.cancel_done = Event()
        self.fail = fail
        self.owner = None

    def attach(self, provider):
        with self.lock:
            self.provider = provider
        if self.stopped.is_set():
            self.cancel()

    def cancel(self):
        self.stopped.set()
        with self.lock:
            provider = self.provider
            if provider is None:
                return
            started = self.cancel_started
            self.cancel_started = True
        if started:
            self.cancel_done.wait()  # Join a concurrent cancel before provider/iterator close.
            return
        try:
            cancel = getattr(provider, "cancel", None)
            if cancel:
                cancel()  # Request-local interrupt; never cancel a shared model instance.
        except BaseException as exc:
            self.fail(exc)
            raise
        finally:
            self.cancel_done.set()


async def _thread_stream(iterator, control):
    """One attributed reader; its owner shields reads and joins before closing."""
    context, sentinel = copy_context(), object()
    try:
        while True:
            read = asyncio.create_task(asyncio.to_thread(context.run, next, iterator, sentinel))
            try:
                value = await asyncio.shield(read)
            except asyncio.CancelledError as exc:
                control.fail(exc)
                control.stopped.set()
                # Even cancellation of an internal owner cannot close a live generator.
                while not read.done():
                    try:
                        await asyncio.shield(read)
                    except asyncio.CancelledError:
                        continue
                raise
            if value is sentinel:
                return
            yield value
    finally:
        try:
            await asyncio.to_thread(context.copy().run, control.cancel)
        except BaseException as exc:
            control.fail(exc)
        try:
            await asyncio.to_thread(context.run, iterator.close)
        except BaseException as exc:
            control.fail(exc)
            raise


class _AnswerStream:
    """Persistent request owner, independent of read/close waiters and generator frames."""

    def __init__(self, runtime, request):
        self._runtime, self._request = runtime, request
        self._context = None
        self._stream = self._read_task = self._close_task = None
        self._started = self._reserved = self._reading = False
        self._succeeded = False
        self._failure = None
        self._failure_lock = Lock()
        self._control = _StreamControl(self._fail)
        self._control.owner = self

    def _fail(self, cause):
        with self._failure_lock:
            if self._failure is None:
                self._failure = AnswerStreamCloseError()
                self._failure.__cause__ = cause

    @property
    def close_completion(self):
        with self._failure_lock:
            state = "failed" if self._failure else "succeeded" if self._succeeded else "unproved"
        return StreamCompletionV1(state=state)

    def __aiter__(self):
        return self

    def _start_close(self):
        self._control.stopped.set()
        if self._close_task is None:
            if self._context is None:
                self._context = copy_context()
            self._close_task = asyncio.create_task(self._close(), context=self._context.copy())
        return self._close_task

    async def aclose(self):
        await asyncio.shield(self._start_close())
        if self._failure is not None:
            raise self._failure

    async def _close(self):
        # Cancellation runs separately from the attributed read context, which may be live.
        try:
            await asyncio.to_thread(self._control.cancel)
        except BaseException as exc:
            self._fail(exc)
        try:
            if self._read_task is not None:
                await asyncio.shield(self._read_task)
        except StopAsyncIteration:
            pass
        except BaseException as exc:
            self._fail(exc)
        try:
            if self._stream is not None:
                await self._stream.aclose()
        except BaseException as exc:
            self._fail(exc)
        # Failed or interrupted disposal retains the reservation and active owner.
        if self._failure is None:
            try:
                if self._reserved:
                    with self._runtime._active_lock:
                        self._runtime.capacity.release_stream()
                        self._runtime._active.discard(self._control)
                    self._reserved = False
                self._succeeded = True
            except BaseException as exc:
                self._fail(exc)
        self._request = None

    async def __anext__(self):
        if self._close_task is not None:
            await self.aclose()
            raise StopAsyncIteration
        if self._reading:
            raise RuntimeError("Application stream already being read")
        self._reading = True
        try:
            if not self._started:
                self._started = True
                self._context = copy_context()
                self._runtime.readiness()
                request = AnswerRequestV1.model_validate(self._request.model_dump())
                if not self._runtime.capacity.acquire_stream():
                    self._start_close()
                    return AnswerEventV1(event="error", data={"code": "capacity_busy"})
                self._reserved = True
                with self._runtime._active_lock:
                    if self._runtime._closed:
                        self._runtime.capacity.release_stream()
                        self._reserved = False
                        raise RuntimeError("Serving snapshot unavailable")
                    self._runtime._active.add(self._control)
                self._stream = _thread_stream(
                    self._runtime._events(request, self._control), self._control
                )
            self._read_task = asyncio.create_task(anext(self._stream), context=self._context.copy())
            try:
                return await asyncio.shield(self._read_task)
            except StopAsyncIteration:
                await self.aclose()
                raise
            except asyncio.CancelledError:
                self._start_close()
                # Repeated interruptions affect this waiter only; the owner keeps draining.
                await asyncio.shield(self._close_task)
                raise
            except BaseException as exc:
                self._fail(exc)
                await self.aclose()
                raise
        except BaseException:
            if self._stream is None:
                await self.aclose()
            raise
        finally:
            self._reading = False


class SnapshotAnswerRuntime:
    """Immutable snapshot with injected retrieval/model dependencies; no env mutation."""

    def __init__(
        self,
        snapshot: ServingSnapshotV1,
        profile,
        retriever,
        llm,
        *,
        max_streams=4,
        preparation_slots=2,
        preparation_wait=2.0,
        capacity: AnswerCapacityV1 | None = None,
        diagnostics: Callable[[Mapping[str, object]], None] | None = None,
        resolver=resolve_messages,
        index_validator=None,
        asset_validator=None,
    ):
        if capacity is not None and (
            not isinstance(capacity, AnswerCapacityV1)
            or (max_streams, preparation_slots, preparation_wait) != (4, 2, 2.0)
        ):
            raise ValueError("Configure explicit shared capacity on its owner")
        self.capacity = capacity or AnswerCapacityV1(
            max_streams, preparation_slots, preparation_wait
        )
        self.snapshot, self.profile, self.retriever, self.llm = snapshot, profile, retriever, llm
        self._streams, self._preparations = self.capacity._streams, self.capacity._preparations
        self._diagnostics, self._ready = diagnostics, False
        self._closed = False
        self._resolver, self._index_validator = resolver, index_validator
        self._asset_validator = asset_validator
        self._active_lock = Lock()
        self._active = set()

    def _verify(self):
        if self._asset_validator:
            self._asset_validator()
        s, c = self.snapshot, self.retriever.context
        if (
            self._closed
            or Path(c.database).resolve() != s.database.resolve()
            or c.corpus_id != s.corpus_id
            or c.chunk_set_id != s.chunk_set_id
            or c.snapshot_sha256 != s.corpus_sha256
            or profile_digest(self.profile) != s.profile_sha256
            or snapshot_digest(s.database) != s.corpus_sha256
        ):
            raise ValueError("Serving snapshot identity mismatch")
        if self.profile.retrieval.enable_dense:
            if not s.vector_collection or not s.embedding_sha256 or not self._index_validator:
                raise ValueError("Dense serving requires index identity")

    async def warmup(self):
        await asyncio.to_thread(self._verify)
        if self._index_validator:
            await asyncio.to_thread(self._index_validator)
        warmup = getattr(self.retriever, "warmup", None)
        if warmup:
            await asyncio.to_thread(warmup)
        with self._active_lock:
            if self._closed:
                raise RuntimeError("Serving snapshot unavailable")
            self._ready = True

    def readiness(self):
        if not self._ready or self._closed:
            raise RuntimeError("Serving snapshot unavailable")
        return self.snapshot.model_dump(mode="json") | {"status": "ready"}

    def _events(self, request, control):
        provider_stream = None
        try:
            self._verify()
            yield AnswerEventV1(event="status", data={"phase": "retrieving_evidence"})
            if control.stopped.is_set():
                return
            if not self.capacity.acquire_preparation(control.stopped):
                if not control.stopped.is_set():
                    yield AnswerEventV1(event="error", data={"code": "capacity_busy"})
                return
            try:
                self._verify()
                if control.stopped.is_set():
                    return
                messages, sources = prepare_answer(
                    request, self.profile, self.retriever, resolver=self._resolver
                )
            finally:
                self.capacity.release_preparation()
            if control.stopped.is_set():
                return
            yield AnswerEventV1(
                event="sources", data={"sources": [s.model_dump() for s in sources]}
            )
            parts = []
            answer_chars = 0
            provider_stream = self.llm.stream_chat(
                messages,
                max_tokens=self.profile.generation.max_tokens,
            )
            control.attach(provider_stream)
            for delta in provider_stream:
                if control.stopped.is_set():
                    return
                if not isinstance(delta, str):
                    raise ValueError("Generation deltas must be text")
                answer_chars += len(delta)
                if len(delta) > 65536 or answer_chars > 262144:
                    raise ValueError("Generation output exceeds application limits")
                parts.append(delta)
                yield AnswerEventV1(event="token", data={"delta": delta})
            if control.stopped.is_set():
                return
            answer = "".join(parts)
            sources = validate_citations(answer, sources)
            metadata = getattr(provider_stream, "get_metadata", lambda: {})()
            if self._diagnostics:
                self._diagnostics({"sources": len(sources), "answer_chars": len(answer)})
            yield AnswerEventV1(
                event="done",
                data={
                    "answer": answer,
                    "sources": [s.model_dump() for s in sources],
                    "corpus_scope": "unified",
                    "usage": metadata.get("usage"),
                    "gateway_attempt_id": metadata.get("gateway_attempt_id"),
                },
            )
        except Exception:
            yield AnswerEventV1(
                event="error",
                data={
                    "code": "generation_error",
                    "message": "Chat request failed",
                },
            )
        finally:
            if provider_stream is not None:
                try:
                    control.cancel()
                except BaseException as exc:
                    control.fail(exc)
                    raise
                finally:
                    try:
                        close = getattr(provider_stream, "close", None)
                        if not callable(close):
                            raise RuntimeError("Request-local provider closure unavailable")
                        close()
                    except BaseException as exc:
                        control.fail(exc)
                        raise

    def stream_answer(self, request) -> AnswerStreamV1:
        return _AnswerStream(self, request)

    async def shutdown(self):
        with self._active_lock:
            self._closed, self._ready = True, False
            active = list(self._active)
        for control in active:
            control.owner._start_close()
