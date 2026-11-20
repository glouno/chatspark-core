import asyncio
import json
import threading
from contextvars import ContextVar
from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from chatspark.application import (
    AnswerRequestV1,
    ServingSnapshotV1,
    create_answer_runtime,
    prepare_answer,
    profile_digest,
)
from chatspark.application.v1 import validate_citations
from chatspark.profiles import load_profile
from chatspark.retrieval.pipeline import execution_context
from chatspark.runtime.registry import get_registry
from chatspark.serve.application import create_http_app
from chatspark.serve.contracts import SourceItem

attribution = ContextVar("test_attribution", default=None)


class Model:
    def __init__(self):
        self.inputs = []
        self.closed = 0
        self.deltas = ("The registration code is ", "ORCHID-42. [C1]")

    def stream_chat(self, messages, *, max_tokens):
        self.inputs.append((messages, attribution.get(), max_tokens))
        try:
            yield from self.deltas
        finally:
            self.closed += 1


def make_runtime(candidate, **options):
    profile = load_profile("example")
    context = execution_context(candidate[0])
    snapshot = ServingSnapshotV1(
        build_id="synthetic",
        database=context.database,
        corpus_id=context.corpus_id,
        chunk_set_id=context.chunk_set_id,
        corpus_sha256=context.snapshot_sha256,
        profile_sha256=profile_digest(profile),
    )
    return create_answer_runtime(
        snapshot, profile=profile, registry=get_registry(allowed=[]), llm=Model(), **options
    )


async def collect(runtime, request):
    return [e async for e in runtime.stream_answer(request)]


@pytest.mark.parametrize("text", ["No relevant evidence.", "", "Referenced [C2] twice [C2]. [C1]"])
def test_completed_citation_table_preserves_labels(text):
    sources = [
        SourceItem(chunk_id=str(i), url=f"https://docs.example/{i}", title=str(i), heading_path=[])
        for i in (1, 2)
    ]
    result = validate_citations(text, sources)
    assert result == (sources if "[C" in text else [])
    assert [s.chunk_id for s in sources] == ["1", "2"]


@pytest.mark.parametrize("text", ["Only the second source [C2].", "Only the first [C1]."])
def test_partially_cited_table_never_compacts_streamed_labels(text):
    sources = [
        SourceItem(chunk_id=str(i), url=f"https://docs.example/{i}", title=str(i), heading_path=[])
        for i in (1, 2, 3)
    ]
    assert validate_citations(text, sources) is sources


@pytest.mark.parametrize("marker", ["[C0]", "[C01]", "[C3]"])
def test_unavailable_citation_cannot_become_an_abstention(marker):
    sources = [
        SourceItem(chunk_id=str(i), url=f"https://docs.example/{i}", title=str(i), heading_path=[])
        for i in (1, 2)
    ]
    with pytest.raises(ValueError, match="Unavailable citation"):
        validate_citations(marker, sources)


def test_uncited_answer_clears_completed_sources_with_http_parity(candidate):
    runtime = make_runtime(candidate)
    runtime.llm.deltas = ("The excerpts do not provide ", "a satellite launch date.")
    request = AnswerRequestV1(question="What is the satellite launch date?")

    async def run():
        await runtime.warmup()
        return await collect(runtime, request)

    events = asyncio.run(run())
    assert [e.event for e in events] == ["status", "sources", "token", "token", "done"]
    assert events[1].data["sources"]  # Retrieval supplied unrelated context to the model.
    assert events[-1].data["sources"] == []
    assert events[-1].data["answer"] == "".join(runtime.llm.deltas)
    assert runtime.llm.closed == 1
    with TestClient(create_http_app(runtime, authorize=lambda request: None)) as client:
        result = client.post("/chat", json={"question": request.question})
        assert result.status_code == 200
        assert result.json() == {k: events[-1].data[k] for k in ("answer", "sources")}
        stream = client.post("/chat/stream", json={"question": request.question})
        done = [p for p in stream.text.split("\n\n") if p.startswith("event: done")][0]
        assert json.loads(done.split("data: ")[1])["sources"] == []
    assert runtime.llm.closed == 3


def test_explicit_runtime_grounding_http_parity_and_attribution(candidate):
    runtime = make_runtime(candidate)
    request = AnswerRequestV1(question="registration code")

    async def run():
        await runtime.warmup()
        token = attribution.set("request-only")
        try:
            return await collect(runtime, request)
        finally:
            attribution.reset(token)

    events = asyncio.run(run())
    assert [e.event for e in events] == ["status", "sources", "token", "token", "done"]
    assert "ORCHID-42" in runtime.llm.inputs[0][0][1]["content"]
    assert runtime.llm.inputs[0][1] == "request-only"
    messages, sources = prepare_answer(request, runtime.profile, runtime.retriever)
    assert messages == runtime.llm.inputs[0][0]
    assert events[-1].data["sources"] == [s.model_dump() for s in sources]

    def authorize(request):
        if request.headers.get("x-test-token") != "test":
            raise HTTPException(403, "Access denied")

    with TestClient(create_http_app(runtime, authorize=authorize)) as client:
        assert "database" not in client.get("/ready").json()
        assert client.post("/chat", json={"question": "registration"}).status_code == 403
        result = client.post(
            "/chat", json={"question": request.question}, headers={"x-test-token": "test"}
        )
        assert result.status_code == 200
        assert result.json() == {k: events[-1].data[k] for k in ("answer", "sources")}
        response = client.post(
            "/chat/stream", json={"question": request.question}, headers={"x-test-token": "test"}
        )
        done = [p for p in response.text.split("\n\n") if p.startswith("event: done")][0]
        assert json.loads(done.split("data: ")[1])["sources"] == events[-1].data["sources"]
    with pytest.raises(RuntimeError):
        runtime.readiness()


@pytest.mark.parametrize("field", ["corpus_id", "chunk_set_id", "profile_sha256", "database"])
def test_foreign_snapshot_rejected_before_model(candidate, tmp_path, field):
    runtime = make_runtime(candidate)
    foreign = tmp_path / "foreign.db"
    foreign.write_bytes(candidate[0].read_bytes())
    value = foreign if field == "database" else "0" * 64
    runtime.snapshot = runtime.snapshot.model_copy(update={field: value})
    with pytest.raises(ValueError, match="identity mismatch"):
        asyncio.run(runtime.warmup())
    assert runtime.llm.inputs == []


def test_filter_widening_and_changed_snapshot_fail_closed(candidate):
    runtime = make_runtime(candidate)
    doc = runtime.retriever.retrieve("registration", 1)[0].metadata["doc_id"]
    runtime.retriever.context = replace(runtime.retriever.context, filters={"doc_id": doc})

    async def run():
        await runtime.warmup()
        bad = await collect(
            runtime, AnswerRequestV1(question="registration", filters={"doc_id": "foreign"})
        )
        assert bad[-1].event == "error"
        assert runtime.llm.inputs == []
        with candidate[0].open("ab") as stream:
            stream.write(b"tampered")
        bad = await collect(runtime, AnswerRequestV1(question="registration"))
        assert [e.event for e in bad] == ["error"]
        assert runtime.llm.inputs == []

    asyncio.run(run())


def test_early_stream_close_releases_provider_and_capacity(candidate):
    runtime = make_runtime(candidate, max_streams=1, preparation_slots=1)
    request = AnswerRequestV1(question="registration")

    async def run():
        await runtime.warmup()
        first = runtime.stream_answer(request)
        assert (await anext(first)).event == "status"
        busy = await collect(runtime, request)
        assert busy[0].data["code"] == "capacity_busy"
        await first.aclose()  # Close before preparation starts.
        second = runtime.stream_answer(request)
        for _ in range(3):
            await anext(second)
        await second.aclose()  # Close with provider stream open.
        assert runtime.llm.closed == 1
        assert (await collect(runtime, request))[-1].event == "done"
        assert runtime.llm.closed == 2

    asyncio.run(run())


def test_slow_status_consumer_does_not_pin_preparation(candidate):
    runtime = make_runtime(candidate, max_streams=2, preparation_slots=1)

    async def run():
        await runtime.warmup()
        first = runtime.stream_answer(AnswerRequestV1(question="registration"))
        assert (await anext(first)).event == "status"
        # Leave the first consumer paused while another request retrieves.
        events = await collect(runtime, AnswerRequestV1(question="registration"))
        assert events[-1].event == "done"
        await first.aclose()

    asyncio.run(run())


def test_provider_output_is_bounded_before_accumulation(candidate):
    runtime = make_runtime(candidate, max_streams=1)

    def overflow(*_args, **_kwargs):
        try:
            for _ in range(8):
                yield "x" * 65536
        finally:
            runtime.llm.closed += 1

    runtime.llm.stream_chat = overflow

    async def run():
        await runtime.warmup()
        events = await collect(runtime, AnswerRequestV1(question="registration"))
        assert events[-1].event == "error"
        assert sum(len(e.data["delta"]) for e in events if e.event == "token") == 262144
        assert runtime.llm.closed == 1
        assert runtime._streams.acquire(blocking=False)
        runtime._streams.release()

    asyncio.run(run())


def test_cancel_interrupts_request_local_provider_read(candidate):
    entered, interrupted = threading.Event(), threading.Event()

    class Stream:
        closed = False

        def __iter__(self):
            return self

        def __next__(self):
            entered.set()
            assert interrupted.wait(5), "Provider was not interrupted"
            raise StopIteration

        def cancel(self):
            interrupted.set()

        def close(self):
            self.closed = True

    provider = Stream()
    runtime = make_runtime(candidate, max_streams=1)
    runtime.llm.stream_chat = lambda *_args, **_kwargs: provider

    async def run():
        await runtime.warmup()
        stream = runtime.stream_answer(AnswerRequestV1(question="registration"))
        assert (await anext(stream)).event == "status"
        assert (await anext(stream)).event == "sources"
        read = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(entered.wait, 2)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, 2)
        assert provider.closed
        assert runtime._streams.acquire(blocking=False)
        runtime._streams.release()

    asyncio.run(run())


def test_shutdown_remains_terminal_when_retriever_warmup_finishes(candidate, monkeypatch):
    runtime = make_runtime(candidate)
    entered, release = threading.Event(), threading.Event()

    def warmup():
        entered.set()
        assert release.wait(5), "Retriever warmup was not released"

    monkeypatch.setattr(runtime.retriever, "warmup", warmup)

    async def run():
        pending = asyncio.create_task(runtime.warmup())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await runtime.shutdown()
            assert runtime._closed and not runtime._ready
        finally:
            release.set()
            with pytest.raises(RuntimeError, match="^Serving snapshot unavailable$"):
                await asyncio.wait_for(pending, 2)

        assert runtime._closed and not runtime._ready
        with pytest.raises(RuntimeError, match="^Serving snapshot unavailable$"):
            runtime.readiness()
        with pytest.raises(RuntimeError, match="^Serving snapshot unavailable$"):
            await collect(runtime, AnswerRequestV1(question="registration"))
        assert not runtime.llm.inputs
        await runtime.shutdown()
        assert runtime._closed and not runtime._ready

    asyncio.run(run())


def test_shutdown_stops_admission_and_drains_pinned_preparation(candidate):
    runtime = make_runtime(candidate, max_streams=1, preparation_slots=1)

    async def run():
        await runtime.warmup()
        stream = runtime.stream_answer(AnswerRequestV1(question="registration"))
        assert (await anext(stream)).event == "status"
        await runtime.shutdown()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not runtime.llm.inputs
        assert runtime._preparations.acquire(blocking=False)
        runtime._preparations.release()
        with pytest.raises(RuntimeError):
            await collect(runtime, AnswerRequestV1(question="registration"))

    asyncio.run(run())


def test_stream_close_capability_survives_exhaustion(candidate):
    from chatspark.application import AnswerStreamV1, StreamCompletionV1

    runtime = make_runtime(candidate, max_streams=1)

    async def run():
        await runtime.warmup()
        stream = runtime.stream_answer(AnswerRequestV1(question="registration"))
        assert isinstance(stream, AnswerStreamV1)
        assert stream.close_completion == StreamCompletionV1(state="unproved")
        events = [event async for event in stream]
        assert events[-1].event == "done"
        assert stream.close_completion.state == "succeeded"
        await stream.aclose()
        assert stream.close_completion.state == "succeeded"
        assert not runtime._active
        await runtime.shutdown()

    asyncio.run(run())


class ClosureProbe:
    """Controlled provider, consumed through the real snapshot/thread bridge."""

    def __init__(self, *, cancel_failure=None, close_failure=None, delayed_cancel=False):
        self.read_entered = threading.Event()
        self.read_release = threading.Event()
        self.read_joined = threading.Event()
        self.cancel_entered = threading.Event()
        self.cancel_release = threading.Event()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.context_exited = threading.Event()
        self.cancel_failure, self.close_failure = cancel_failure, close_failure
        self.cancel_calls = self.close_calls = 0
        self.cancel_context = self.close_context = None
        self.reading = False
        self.iterator = self.tokens()
        if not delayed_cancel:
            self.cancel_release.set()

    def tokens(self):
        token = attribution.set("provider-context")
        try:
            yield "The registration code is "
            self.reading = True
            self.read_entered.set()
            try:
                assert self.read_release.wait(5), "Owned read barrier was not released"
            finally:
                self.reading = False
                self.read_joined.set()
            yield "ORCHID-42. [C1]"
        finally:
            attribution.reset(token)
            self.context_exited.set()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iterator)

    def cancel(self):
        self.cancel_calls += 1
        self.cancel_context = attribution.get()
        self.cancel_entered.set()
        assert self.cancel_release.wait(5), "Owned cancel barrier was not released"
        if self.cancel_failure is not None:
            raise self.cancel_failure

    def close(self):
        self.close_calls += 1
        self.close_context = attribution.get()
        assert not self.reading, "Closed a live read"
        self.close_entered.set()
        assert self.close_release.wait(5), "Owned close barrier was not released"
        assert self.cancel_release.is_set(), "Provider close raced cancel"
        self.iterator.close()
        if self.close_failure is not None:
            raise self.close_failure

    def release(self):
        self.cancel_release.set()
        self.read_release.set()
        self.close_release.set()


async def open_probe(runtime, provider):
    runtime.llm.stream_chat = lambda *_args, **_kwargs: provider
    await runtime.warmup()
    token = attribution.set("request-context")
    try:
        stream = runtime.stream_answer(AnswerRequestV1(question="registration"))
        assert [(await anext(stream)).event for _ in range(3)] == ["status", "sources", "token"]
    finally:
        attribution.reset(token)
    assert attribution.get() is None
    return stream


def assert_reserved(runtime, stream, state="unproved"):
    assert stream.close_completion.state == state
    assert len(runtime._active) == 1
    assert not runtime.capacity.acquire_stream()


@pytest.mark.parametrize("interruptions", [1, 3])
def test_native_read_cancellation_owned_close_survives_waiters(candidate, interruptions):
    runtime = make_runtime(candidate, max_streams=1)
    provider = ClosureProbe(delayed_cancel=True)

    async def run():
        stream = await open_probe(runtime, provider)
        read = asyncio.create_task(anext(stream))
        waiters = []
        try:
            assert await asyncio.to_thread(provider.read_entered.wait, 2)
            read.cancel()
            assert await asyncio.to_thread(provider.cancel_entered.wait, 2)
            assert_reserved(runtime, stream)
            for _ in range(interruptions):
                waiter = asyncio.create_task(stream.aclose())
                waiters.append(waiter)
                await asyncio.sleep(0)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                read.cancel()
                await asyncio.sleep(0)
                assert_reserved(runtime, stream)
            provider.read_release.set()
            assert await asyncio.to_thread(provider.read_joined.wait, 2)
            assert not provider.close_entered.is_set()  # Cancel must join first.
            assert_reserved(runtime, stream)
            provider.cancel_release.set()
            assert await asyncio.to_thread(provider.close_entered.wait, 2)
            assert_reserved(runtime, stream)
            provider.close_release.set()
            await asyncio.wait_for(stream.aclose(), 2)
            with pytest.raises(asyncio.CancelledError):
                await read
            await stream.aclose()
            assert stream.close_completion.state == "succeeded"
            assert provider.read_joined.is_set() and provider.context_exited.is_set()
            assert provider.cancel_calls == provider.close_calls == 1
            assert provider.cancel_context == "request-context"
            assert provider.close_context == "provider-context"
            assert attribution.get() is None
            assert not runtime._active
            assert runtime.capacity.acquire_stream()
            runtime.capacity.release_stream()
        finally:
            provider.release()
            await asyncio.gather(read, *waiters, return_exceptions=True)
            await runtime.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("failure_at", ["cancel", "close", "both", "cancelled"])
def test_native_close_failure_is_sticky_and_retains_lifetime(candidate, failure_at):
    from chatspark.application import AnswerStreamCloseError

    original_cancel = (
        asyncio.CancelledError("controlled sensitive cancel detail")
        if failure_at == "cancelled"
        else RuntimeError("controlled sensitive cancel detail")
    )
    original_close = RuntimeError("controlled sensitive close detail")
    provider = ClosureProbe(
        cancel_failure=original_cancel if failure_at in {"cancel", "both", "cancelled"} else None,
        close_failure=original_close if failure_at in {"close", "both"} else None,
    )
    runtime = make_runtime(candidate, max_streams=1)

    async def run():
        stream = await open_probe(runtime, provider)
        read = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(provider.read_entered.wait, 2)
            read.cancel()
            assert await asyncio.to_thread(provider.cancel_entered.wait, 2)
            assert_reserved(runtime, stream, "failed" if failure_at != "close" else "unproved")
            provider.read_release.set()
            assert await asyncio.to_thread(provider.close_entered.wait, 2)
            provider.close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await read
            failures = []
            for _ in range(3):
                with pytest.raises(AnswerStreamCloseError) as caught:
                    await stream.aclose()
                failures.append(caught.value)
            assert failures[0] is failures[1] is failures[2]
            assert failures[0].__cause__ is (
                original_close if failure_at == "close" else original_cancel
            )
            assert "sensitive" not in repr(failures[0])
            assert "sensitive" not in stream.close_completion.model_dump_json()
            assert_reserved(runtime, stream, "failed")
            assert provider.read_joined.is_set() and provider.context_exited.is_set()
            assert provider.cancel_calls == provider.close_calls == 1
            busy = await collect(runtime, AnswerRequestV1(question="registration"))
            assert busy[-1].data == {"code": "capacity_busy"}
            await runtime.shutdown()
            assert_reserved(runtime, stream, "failed")
        finally:
            provider.release()
            await asyncio.gather(read, return_exceptions=True)

    asyncio.run(run())


def test_native_explicit_close_joins_inflight_read_without_canceling_reader(candidate):
    runtime = make_runtime(candidate, max_streams=1)
    provider = ClosureProbe()

    async def run():
        stream = await open_probe(runtime, provider)
        read = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(provider.read_entered.wait, 2)
            closer = asyncio.create_task(stream.aclose())
            assert await asyncio.to_thread(provider.cancel_entered.wait, 2)
            closer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closer
            assert_reserved(runtime, stream)
            provider.read_release.set()
            provider.close_release.set()
            await stream.aclose()
            with pytest.raises(StopAsyncIteration):
                await read
            assert stream.close_completion.state == "succeeded"
            assert provider.context_exited.is_set()
            assert not runtime._active
        finally:
            provider.release()
            await asyncio.gather(read, return_exceptions=True)
            await runtime.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("failed", [False, True])
def test_native_natural_exhaustion_proves_only_successful_cleanup(candidate, failed):
    from chatspark.application import AnswerStreamCloseError

    original = RuntimeError("controlled sensitive natural close detail")
    provider = ClosureProbe(close_failure=original if failed else None)
    provider.release()
    runtime = make_runtime(candidate, max_streams=1)

    async def run():
        stream = await open_probe(runtime, provider)
        assert (await anext(stream)).event == "token"
        assert (await anext(stream)).event == "done"
        assert_reserved(runtime, stream)
        if failed:
            with pytest.raises(AnswerStreamCloseError) as first:
                await anext(stream)
            with pytest.raises(AnswerStreamCloseError) as repeated:
                await stream.aclose()
            assert first.value is repeated.value
            assert first.value.__cause__ is original
            assert_reserved(runtime, stream, "failed")
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            await asyncio.gather(stream.aclose(), stream.aclose(), stream.aclose())
            assert stream.close_completion.state == "succeeded"
            assert not runtime._active
        assert provider.cancel_calls == provider.close_calls == 1
        assert provider.context_exited.is_set()
        assert attribution.get() is None
        await runtime.shutdown()

    asyncio.run(run())


def test_native_paused_close_waiter_cancellation_and_unstarted_close(candidate):
    runtime = make_runtime(candidate, max_streams=1)
    provider = ClosureProbe()

    async def run():
        stream = await open_probe(runtime, provider)
        first = asyncio.create_task(stream.aclose())
        try:
            assert await asyncio.to_thread(provider.close_entered.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert_reserved(runtime, stream)
            provider.close_release.set()
            await asyncio.gather(stream.aclose(), stream.aclose())
            assert stream.close_completion.state == "succeeded"
            assert provider.cancel_calls == provider.close_calls == 1
            assert provider.context_exited.is_set()
            unopened = runtime.stream_answer(AnswerRequestV1(question="registration"))
            await unopened.aclose()
            assert unopened.close_completion.state == "succeeded"
            with pytest.raises(StopAsyncIteration):
                await anext(unopened)
            assert not runtime._active
        finally:
            provider.release()
            await asyncio.gather(first, return_exceptions=True)
            await runtime.shutdown()

    asyncio.run(run())


def test_provider_without_close_cannot_manufacture_closure_proof(candidate):
    from chatspark.application import AnswerStreamCloseError

    runtime = make_runtime(candidate, max_streams=1)
    runtime.llm.stream_chat = lambda *_args, **_kwargs: iter(["ORCHID-42. [C1]"])

    async def run():
        await runtime.warmup()
        stream = runtime.stream_answer(AnswerRequestV1(question="registration"))
        assert [(await anext(stream)).event for _ in range(4)] == [
            "status",
            "sources",
            "token",
            "done",
        ]
        with pytest.raises(AnswerStreamCloseError):
            await anext(stream)
        with pytest.raises(AnswerStreamCloseError):
            await stream.aclose()
        assert_reserved(runtime, stream, "failed")
        await runtime.shutdown()

    asyncio.run(run())


def test_shutdown_owns_close_until_delayed_provider_disposal(candidate):
    runtime = make_runtime(candidate, max_streams=1)
    provider = ClosureProbe()

    async def run():
        stream = await open_probe(runtime, provider)
        try:
            await runtime.shutdown()
            assert await asyncio.to_thread(provider.close_entered.wait, 2)
            assert_reserved(runtime, stream)
            with pytest.raises(RuntimeError, match="Serving snapshot unavailable"):
                runtime.readiness()
            provider.close_release.set()
            await stream.aclose()
            assert stream.close_completion.state == "succeeded"
            assert not runtime._active
            assert provider.context_exited.is_set()
            assert provider.cancel_calls == provider.close_calls == 1
        finally:
            provider.release()
            await stream.aclose()

    asyncio.run(run())
