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
from chatspark.profiles import load_profile
from chatspark.retrieval.pipeline import execution_context
from chatspark.runtime.registry import get_registry
from chatspark.serve.application import create_http_app

attribution = ContextVar("test_attribution", default=None)


class Model:
    def __init__(self):
        self.inputs = []
        self.closed = 0

    def stream_chat(self, messages, *, max_tokens):
        self.inputs.append((messages, attribution.get(), max_tokens))
        try:
            yield "The registration code is "
            yield "ORCHID-42. [C1]"
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
        await first.aclose()  # Close while preparation is pinned.
        second = runtime.stream_answer(request)
        for _ in range(3):
            await anext(second)
        await second.aclose()  # Close with provider stream open.
        assert runtime.llm.closed == 1
        assert (await collect(runtime, request))[-1].event == "done"
        assert runtime.llm.closed == 2

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
