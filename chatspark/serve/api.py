import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from chatspark.generation.client import generate, stream_generate
from chatspark.generation.prompts import resolve_messages
from chatspark.profiles import load_profile
from chatspark.runtime.composition import selected_runtime
from chatspark.runtime.config import settings
from chatspark.runtime.factory import get_retriever
from chatspark.runtime.validation import validate_runtime
from chatspark.serve.contracts import ChatRequest, ChatResponse, SourceItem


@asynccontextmanager
async def lifespan(app):
    validate_runtime(load_profile(settings.CHATSPARK_PROFILE), require_dense=True)
    if settings.CHATSPARK_SERVE_PRELOAD:
        if (
            not settings.CHATSPARK_V3_DB_PATH
            or hashlib.sha256(settings.CHATSPARK_V3_DB_PATH.read_bytes()).hexdigest()
            != settings.CHATSPARK_SERVE_CORPUS_SHA256
        ):
            raise RuntimeError("Serving snapshot does not match the release")
        get_retriever()
    yield


app = FastAPI(title="ChatSpark Core", lifespan=lifespan)


def authorized(request):
    token = settings.CHATSPARK_API_PROXY_SHARED_TOKEN
    if token:
        supplied = request.headers.get("x-chatspark-proxy-token", "")
        if not hmac.compare_digest(token, supplied):
            raise HTTPException(403, "Access denied")
    elif not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(403, "Configure authentication before remote use")


def prepare(payload):
    profile = load_profile(settings.CHATSPARK_PROFILE)
    retriever = get_retriever(profile=profile)
    result = retriever.retrieve_result(
        payload.question, payload.top_k or profile.retrieval.final_k, payload.filters
    )
    from dataclasses import replace

    from chatspark.storage.evidence import narrow_filters

    generation_context = replace(
        retriever.context, filters=narrow_filters(retriever.context.filters, payload.filters or {})
    )
    messages, prompt_metadata = resolve_messages(
        payload.question, result.items, profile, context=generation_context
    )
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
        if c.chunk_id in prompt_metadata["included_evidence_ids"]
    ]
    return profile, messages, sources


def validate_citations(text, sources):
    available = {f"C{i}" for i in range(1, len(sources) + 1)}
    if not set(re.findall(r"\[(C\d+)\]", text)) <= available:
        raise RuntimeError("Generation returned an unavailable citation")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    try:
        get_retriever()
    except Exception:
        raise HTTPException(503, "Serving snapshot unavailable") from None
    return {"status": "ready", "build_id": settings.CHATSPARK_SERVE_BUILD_ID}


@app.get("/")
@app.get("/app", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        """<!doctype html><html lang="en"><meta charset="utf-8"><title>ChatSpark Core</title><h1>ChatSpark Core</h1><p>Ask about the configured corpus.</p><form id="chat"><input id="question" required aria-label="Question"><button>Ask</button></form><pre id="answer"></pre><script>document.querySelector('#chat').onsubmit=async e=>{e.preventDefault();const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:document.querySelector('#question').value})});document.querySelector('#answer').textContent=JSON.stringify(await r.json(),null,2)};</script></html>"""
    )


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, request: Request):
    authorized(request)
    try:
        profile, messages, sources = prepare(payload)
        text = generate(messages, max_tokens=profile.generation.max_tokens)
        validate_citations(text, sources)
        return ChatResponse(answer=text, sources=sources)
    except Exception:
        raise HTTPException(502, "Chat request failed") from None


def event(name, value):
    return "event: " + name + "\ndata: " + json.dumps(value) + "\n\n"


@app.post("/chat/stream")
def stream(payload: ChatRequest, request: Request):
    authorized(request)

    def events():
        try:
            yield event("status", {"phase": "retrieving_evidence"})
            profile, messages, sources = prepare(payload)
            source_data = [s.model_dump() for s in sources]
            yield event("sources", {"sources": source_data})
            parts = []
            for delta in stream_generate(messages, max_tokens=profile.generation.max_tokens):
                parts.append(delta)
                yield event("token", {"delta": delta})
            text = "".join(parts)
            validate_citations(text, sources)
            yield event("done", {"answer": text, "sources": source_data, "corpus_scope": "unified"})
        except Exception:
            yield event("error", {"message": "Chat request failed"})

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


_runtime = selected_runtime()
if _runtime is not None:
    app = _runtime.http_app()
