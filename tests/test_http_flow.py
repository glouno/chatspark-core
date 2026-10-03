import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from chatspark.runtime.config import settings
from chatspark.serve.api import app


def test_real_mock_endpoint_grounded_answer_and_stream(candidate, monkeypatch, caplog):
    database, _ = candidate

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "ORCHID-42" in data["messages"][1]["content"]
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if data["stream"] else "application/json"
            )
            self.end_headers()
            text = "The registration code is ORCHID-42. [C1]"
            if data["stream"]:
                for delta in ("The registration code is ", "ORCHID-42. [C1]"):
                    self.wfile.write(
                        (
                            "data: "
                            + json.dumps({"choices": [{"delta": {"content": delta}}]})
                            + "\n\n"
                        ).encode()
                    )
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(json.dumps({"choices": [{"message": {"content": text}}]}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "CHATSPARK_V3_DB_PATH", database)
    monkeypatch.setattr(
        settings, "OPENAI_COMPAT_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1"
    )
    monkeypatch.setattr(settings, "OPENAI_COMPAT_MODEL", "synthetic-mock")
    try:
        with TestClient(app) as client:
            response = client.post(
                "/chat",
                json={"question": "registration code", "filters": {}, "corpus_scope": "unified"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["sources"][0]["chunk_id"]
            stream = client.post("/chat/stream", json={"question": "registration code"})
            assert "event: token" in stream.text and "event: done" in stream.text
            assert "event: error" not in stream.text
        assert "ORCHID-42" not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_proxy_auth_and_safe_error(candidate, monkeypatch):
    monkeypatch.setattr(settings, "CHATSPARK_API_PROXY_SHARED_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(settings, "CHATSPARK_V3_DB_PATH", candidate[0])
    monkeypatch.setattr(settings, "OPENAI_COMPAT_MODEL", "")
    with TestClient(app) as client:
        assert client.post("/chat", json={"question": "registration"}).status_code == 403
        response = client.post(
            "/chat",
            json={"question": "registration"},
            headers={"x-chatspark-proxy-token": "synthetic-test-token"},
        )
        assert response.status_code == 502
        assert response.json() == {"detail": "Chat request failed"}


def test_generation_revalidation_uses_request_authorization(candidate, monkeypatch):
    from chatspark.profiles import load_profile
    from chatspark.runtime.factory import get_retriever
    from chatspark.serve import api
    from chatspark.serve.contracts import ChatRequest

    retriever = get_retriever(database=candidate[0], profile=load_profile("example"))
    authorized = retriever.retrieve("registration", 3)[0]
    monkeypatch.setattr(api, "get_retriever", lambda **kwargs: retriever)
    original = api.resolve_messages

    def resolve(question, chunks, profile, *, context):
        assert context.filters == {"doc_id": authorized.metadata["doc_id"]}
        return original(question, chunks, profile, context=context)

    monkeypatch.setattr(api, "resolve_messages", resolve)
    api.prepare(
        ChatRequest(question="registration", filters={"doc_id": authorized.metadata["doc_id"]})
    )
