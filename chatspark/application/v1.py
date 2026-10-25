"""Application API v1. Callers authorize access; the engine verifies evidence."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chatspark.generation.prompts import resolve_messages
from chatspark.profiles.loader import profile_digest
from chatspark.serve.contracts import SourceItem
from chatspark.storage.evidence import narrow_filters, snapshot_digest


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
    available = {f"C{i}" for i in range(1, len(sources) + 1)}
    if not set(re.findall(r"\[(C\d+)\]", text)) <= available:
        raise ValueError("Unavailable citation")


class _StreamControl:
    def __init__(self):
        self.stopped = Event()
        self.lock = Lock()
        self.provider = None

    def attach(self, provider):
        with self.lock:
            self.provider = provider
        if self.stopped.is_set():
            self.cancel()

    def cancel(self):
        self.stopped.set()
        with self.lock:
            provider = self.provider
        cancel = getattr(provider, "cancel", None)
        if cancel:
            with suppress(Exception):
                cancel()  # Request-local interrupt; never cancel a shared model instance.


async def _thread_stream(iterator, control):
    """Preserve attribution across thread reads and close on every exit path."""
    context, sentinel = copy_context(), object()
    try:
        while True:
            task = asyncio.create_task(asyncio.to_thread(context.run, next, iterator, sentinel))
            try:
                value = await asyncio.shield(task)
            except asyncio.CancelledError:
                control.cancel()
                await task  # A running synchronous read must finish before close.
                raise
            if value is sentinel:
                return
            yield value
    finally:
        control.cancel()
        task = asyncio.create_task(asyncio.to_thread(context.run, iterator.close))
        await asyncio.shield(task)


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
        diagnostics: Callable[[Mapping[str, object]], None] | None = None,
        resolver=resolve_messages,
        index_validator=None,
        asset_validator=None,
    ):
        if max_streams < 1 or preparation_slots < 1 or preparation_wait < 0:
            raise ValueError("Invalid capacity configuration")
        self.snapshot, self.profile, self.retriever, self.llm = snapshot, profile, retriever, llm
        self._streams = BoundedSemaphore(max_streams)
        self._preparations = BoundedSemaphore(preparation_slots)
        self._wait, self._diagnostics, self._ready = preparation_wait, diagnostics, False
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
            if not self._preparations.acquire(timeout=self._wait):
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
                self._preparations.release()
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
            validate_citations(answer, sources)
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
            if provider_stream is not None and hasattr(provider_stream, "close"):
                provider_stream.close()

    async def stream_answer(self, request):
        self.readiness()
        request = AnswerRequestV1.model_validate(request.model_dump())
        if not self._streams.acquire(blocking=False):
            yield AnswerEventV1(event="error", data={"code": "capacity_busy"})
            return
        control = _StreamControl()
        with self._active_lock:
            if self._closed:
                self._streams.release()
                raise RuntimeError("Serving snapshot unavailable")
            self._active.add(control)
        stream = _thread_stream(self._events(request, control), control)
        try:
            async for event in stream:
                yield event
        finally:
            try:
                await stream.aclose()
            finally:
                with self._active_lock:
                    self._active.discard(control)
                self._streams.release()

    async def shutdown(self):
        with self._active_lock:
            self._closed, self._ready = True, False
            active = list(self._active)
        for control in active:
            control.cancel()
