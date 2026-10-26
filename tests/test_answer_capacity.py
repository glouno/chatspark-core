"""Shared host limits without sharing corpus configuration or request content."""

import asyncio
import threading
import time

import pytest
from test_application_runtime import collect, make_runtime

from chatspark.application import AnswerCapacityV1, AnswerRequestV1


@pytest.mark.parametrize(
    "options",
    [
        {"max_streams": 0},
        {"max_streams": True},
        {"preparation_slots": 1.5},
        {"preparation_wait": -1},
        {"preparation_wait": float("inf")},
        {"preparation_wait": float("nan")},
        {"preparation_wait": True},
        {"preparation_wait": 121},
    ],
)
def test_invalid_capacity(options):
    with pytest.raises(ValueError):
        AnswerCapacityV1(**options)


def test_timeout_cancellation_and_repeated_recovery():
    capacity = AnswerCapacityV1(preparation_slots=1, preparation_wait=0.1)
    live = threading.Event()
    for _ in range(3):
        assert capacity.acquire_preparation(live)
        started = time.monotonic()
        assert not capacity.acquire_preparation(live)
        assert 0.08 <= time.monotonic() - started < 1
        cancelled = threading.Event()
        cancelled.set()
        assert not capacity.acquire_preparation(cancelled)
        capacity.release_preparation()
        assert not capacity._waiting
    with pytest.raises(ValueError):
        capacity.release_preparation()
    with pytest.raises(ValueError):
        capacity.release_stream()


def test_fifo_and_cancelled_head_does_not_block_followers():
    capacity = AnswerCapacityV1(preparation_slots=1, preparation_wait=2)
    assert capacity.acquire_preparation(threading.Event())
    order, threads, cancelled = [], [], [threading.Event() for _ in range(3)]

    def wait(number):
        if capacity.acquire_preparation(cancelled[number]):
            order.append(number)
            capacity.release_preparation()

    try:
        for number in range(3):
            thread = threading.Thread(target=wait, args=(number,))
            thread.start()
            threads.append(thread)
            deadline = time.monotonic() + 1
            while True:
                with capacity._condition:
                    ready = len(capacity._waiting) == number + 1
                if ready:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.001)
        cancelled[0].set()
        threads[0].join(timeout=0.5)
        assert not threads[0].is_alive()
        capacity.release_preparation()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        assert order == [1, 2] and not capacity._waiting
    finally:
        for event in cancelled:
            event.set()
        for thread in threads:
            thread.join(timeout=3)


def test_two_runtimes_share_stream_limit_and_shutdown_remains_local(candidate):
    capacity = AnswerCapacityV1(max_streams=1)
    first, second = [make_runtime(candidate, capacity=capacity) for _ in range(2)]
    request = AnswerRequestV1(question="registration code")

    async def run():
        await asyncio.gather(first.warmup(), second.warmup())
        stream = first.stream_answer(request)
        await anext(stream)
        busy = await collect(second, request)
        assert busy[0].data == {"code": "capacity_busy"} and not second.llm.inputs
        await first.shutdown()
        await stream.aclose()
        assert (await collect(second, request))[-1].event == "done"
        await second.shutdown()
        assert capacity.acquire_stream()
        capacity.release_stream()

    asyncio.run(run())


def test_two_runtimes_share_preparation_before_model_input(candidate):
    capacity = AnswerCapacityV1(preparation_slots=1, preparation_wait=0.1)
    first, second = [make_runtime(candidate, capacity=capacity) for _ in range(2)]
    entered, release = threading.Event(), threading.Event()
    original = first._resolver

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=3):
            raise RuntimeError("synthetic preparation timeout")
        return original(*args, **kwargs)

    first._resolver = blocked
    request = AnswerRequestV1(question="registration code")

    async def run():
        await asyncio.gather(first.warmup(), second.warmup())
        stream = first.stream_answer(request)
        assert (await anext(stream)).event == "status"
        preparation = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            busy = await collect(second, request)
            assert busy[-1].data == {"code": "capacity_busy"} and not second.llm.inputs
            release.set()
            assert (await preparation).event == "sources"
            await stream.aclose()
            assert (await collect(second, request))[-1].event == "done"
        finally:
            release.set()
            if not preparation.done():
                await preparation
            await stream.aclose()
            await asyncio.gather(first.shutdown(), second.shutdown())

    asyncio.run(run())


def test_cancellation_does_not_spend_full_preparation_wait(candidate):
    capacity = AnswerCapacityV1(max_streams=1, preparation_slots=1, preparation_wait=2)
    runtime = make_runtime(candidate, capacity=capacity)
    assert capacity.acquire_preparation(threading.Event())

    async def run():
        await runtime.warmup()
        stream = runtime.stream_answer(AnswerRequestV1(question="registration code"))
        await anext(stream)
        waiting = asyncio.create_task(anext(stream))
        deadline = time.monotonic() + 1
        while not capacity._waiting:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.001)
        started = time.monotonic()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await stream.aclose()
        assert time.monotonic() - started < 1
        assert not capacity._waiting and not runtime.llm.inputs
        assert capacity.acquire_stream()
        capacity.release_stream()
        await runtime.shutdown()

    try:
        asyncio.run(run())
    finally:
        capacity.release_preparation()


def test_explicit_capacity_rejects_conflicting_legacy_parameters(candidate):
    with pytest.raises(ValueError, match="explicit shared capacity"):
        make_runtime(candidate, capacity=AnswerCapacityV1(), preparation_slots=3)
