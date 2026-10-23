"""HTTP adapter for an explicitly constructed application runtime."""

import json
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from chatspark.application import AnswerRequestV1, AnswerRuntime
from chatspark.serve.contracts import ChatRequest, ChatResponse


def create_http_app(runtime: AnswerRuntime, *, authorize: Callable[[Request], None]) -> FastAPI:
    """The host supplies authentication; this adapter never accepts a corpus selector."""

    @asynccontextmanager
    async def lifespan(app):
        await runtime.warmup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(title="ChatSpark", lifespan=lifespan)

    def answer_request(payload):
        return AnswerRequestV1(
            question=payload.question, filters=payload.filters or {}, top_k=payload.top_k
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        try:
            return runtime.readiness()
        except Exception:
            raise HTTPException(503, "Serving snapshot unavailable") from None

    @app.post("/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest, request: Request):
        authorize(request)
        stream = runtime.stream_answer(answer_request(payload))
        try:
            async for event in stream:
                if event.event == "error":
                    raise HTTPException(502, "Chat request failed")
                if event.event == "done":
                    return ChatResponse(answer=event.data["answer"], sources=event.data["sources"])
            raise HTTPException(502, "Chat request failed")
        finally:
            await stream.aclose()

    @app.post("/chat/stream")
    async def stream(payload: ChatRequest, request: Request):
        authorize(request)

        async def events():
            stream = runtime.stream_answer(answer_request(payload))
            try:
                async for event in stream:
                    yield "event: " + event.event + "\ndata: " + json.dumps(event.data) + "\n\n"
            finally:
                await stream.aclose()

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )

    return app
