import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chatspark.engine.build import execute_build
from chatspark.engine.contracts import EngineBuildRequestV1
from chatspark.ingestion.acquire import acquire
from chatspark.profiles import load_profile
from chatspark.runtime.factory import get_retriever


def test_authorized_local_crawl_build_retrieve(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html"
            )
            self.end_headers()
            self.wfile.write(
                b"User-agent: *\nAllow: /\n"
                if self.path == "/robots.txt"
                else b"<html><title>Workshop</title><article><h1>Registration</h1><p>The workshop registration code is ORCHID-42. Use this code to register for the synthetic workshop.</p></article></html>"
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = load_profile("example")
    profile.crawl.start_urls = [f"http://127.0.0.1:{server.server_port}/workshop"]
    profile.crawl.allowed_domains = ["127.0.0.1"]
    profile.crawl.download_delay_seconds = 0
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {"acquisition_enabled": True, "authorization_reference": "synthetic-loopback-fixture"}
        )
    )
    try:
        with pytest.raises(ValueError, match="Non-public"):
            acquire(profile, tmp_path / "blocked", policy)
        report = acquire(profile, tmp_path / "sources", policy, allow_loopback=True)
        assert report["documents"] == 1
        result = execute_build(
            EngineBuildRequestV1(
                build_id="crawl",
                source_root=tmp_path / "sources",
                output_root=tmp_path / "candidate",
                profile="example",
            )
        )
        assert result.status == "succeeded", result.error
        chunks = get_retriever(
            database=tmp_path / "candidate/corpus.db", chunk_set_id=result.chunk_set_id
        ).retrieve("registration", 3)
        assert "ORCHID-42" in chunks[0].content
        assert chunks[0].metadata["url"].endswith("/workshop")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_acquisition_disabled_before_network(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text('{"acquisition_enabled":false}')
    with pytest.raises(ValueError, match="enabled policy"):
        acquire(load_profile("example"), tmp_path / "sources", policy)


def test_configured_concurrency_is_bounded_and_used(tmp_path):
    import time

    active = peak = 0
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal active, peak
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html"
            )
            self.end_headers()
            if self.path == "/robots.txt":
                self.wfile.write(b"User-agent: *\nAllow: /\n")
                return
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.15)
            self.wfile.write(
                b"<main><h1>Guide</h1><p>Authored workshop documentation for testing concurrent acquisition.</p></main>"
            )
            with lock:
                active -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = load_profile("example")
    profile.crawl.allowed_domains = ["127.0.0.1"]
    profile.crawl.start_urls = [f"http://127.0.0.1:{server.server_port}/{i}" for i in range(4)]
    profile.crawl.download_delay_seconds = 0
    profile.crawl.concurrent_requests_per_domain = 2
    profile.crawl.autothrottle_target_concurrency = 2
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({"acquisition_enabled": True, "authorization_reference": "synthetic"})
    )
    try:
        report = acquire(profile, tmp_path / "sources", policy, allow_loopback=True)
        assert report["documents"] == 4
        assert peak == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
