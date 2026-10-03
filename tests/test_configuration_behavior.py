"""Exercise operator-visible configuration through builds and acquisition."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chatspark.documents.urls import canonicalize_source_url
from chatspark.engine.build import execute_build
from chatspark.engine.contracts import EngineBuildRequestV1
from chatspark.ingestion.acquire import acquire
from chatspark.profiles import load_profile
from chatspark.profiles.models import CanonicalURLRewrite
from chatspark.runtime.factory import get_retriever


@pytest.mark.parametrize(
    "rule,url,expected",
    [
        (
            {"from_host": "old.test", "to_host": "new.test"},
            "https://old.test/guide",
            "https://new.test/guide",
        ),
        (
            {"from_host": "other.test", "to_host": "new.test"},
            "https://old.test/guide",
            "https://old.test/guide",
        ),
        ({"from_path": "/old", "to_path": "/new"}, "https://old.test/old", "https://old.test/new"),
        (
            {"from_path": "/other", "to_path": "/new"},
            "https://old.test/old",
            "https://old.test/old",
        ),
        (
            {"from_path_prefix": "/old/", "to_path_prefix": "/new/"},
            "https://old.test/old/guide?q=1",
            "https://old.test/new/guide?q=1",
        ),
        (
            {"from_path_prefix": "/other/", "to_path_prefix": "/new/"},
            "https://old.test/old/guide",
            "https://old.test/old/guide",
        ),
    ],
)
def test_canonical_host_exact_path_and_prefix_controls(rule, url, expected):
    assert canonicalize_source_url(url, [CanonicalURLRewrite(**rule)]) == expected


def test_build_uses_selected_rewrites_not_global_profile(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "guide.md").write_text("# Workshop\n\nThe registration code is ORCHID-42.\n")
    profile = load_profile("example")
    profile.corpus.name = "selected-workshop"
    profile.corpus.display_name = "Synthetic Workshop"
    profile.corpus.description = "Authored configuration fixture"
    profile.crawl.canonical_url_rewrites = [
        CanonicalURLRewrite(from_host="old.test", to_host="new.test"),
        CanonicalURLRewrite(from_path_prefix="/old/", to_path_prefix="/new/"),
    ]
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    result = execute_build(
        EngineBuildRequestV1(
            build_id="rewrite",
            source_root=source,
            source_base_url="https://old.test/old",
            output_root=tmp_path / "candidate",
            profile=str(path),
        )
    )
    assert result.status == "succeeded", result.error
    database = tmp_path / "candidate/corpus.db"
    items = get_retriever(database=database, profile=profile).retrieve("registration", 3)
    assert items[0].metadata["url"] == "https://new.test/new/guide.md"
    import sqlite3

    with sqlite3.connect(database) as con:
        row = con.execute("SELECT name, metadata_json FROM corpora").fetchone()
    assert row[0] == "selected-workshop"
    assert json.loads(row[1]) == {
        "display_name": "Synthetic Workshop",
        "description": "Authored configuration fixture",
    }


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({"allow_patterns": ["/good$"]}, 1),
        ({"deny_patterns": ["/good$"]}, 0),
        ({"deny_domains": ["127.0.0.1"]}, None),
        ({"html_ignored_extensions": [".skip"]}, 0),
        ({"files_ignored_extensions": [".bin"]}, 0),
        ({"file_link_allow_patterns": ["/approved$"]}, 0),
        ({"blocked_content_types": ["text/html", "application/pdf"]}, 0),
        ({"max_pages": 1}, 1),
    ],
)
def test_crawl_source_and_mime_controls(settings, expected, tmp_path):
    observed_agents = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            observed_agents.append(self.headers.get("User-Agent"))
            self.send_response(200)
            mime = "text/plain" if self.path == "/robots.txt" else "text/html"
            if self.path == "/file.bin":
                mime = "application/pdf"
            self.send_header("Content-Type", mime)
            self.end_headers()
            self.wfile.write(
                b"User-agent: *\nAllow: /\n"
                if self.path == "/robots.txt"
                else b"<main><h1>Workshop</h1><p>The registration code is ORCHID-42.</p></main>"
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = load_profile("example")
    profile.crawl.allowed_domains = ["127.0.0.1"]
    suffix = (
        "/file.bin"
        if "files_ignored_extensions" in settings or "file_link_allow_patterns" in settings
        else "/good.skip"
        if "html_ignored_extensions" in settings
        else "/good"
    )
    profile.crawl.start_urls = [f"http://127.0.0.1:{server.server_port}{suffix}"]
    if "max_pages" in settings:
        profile.crawl.start_urls.append(f"http://127.0.0.1:{server.server_port}/other")
    profile.crawl.download_delay_seconds = 0
    profile.crawl.user_agent = "ChatSparkSyntheticTest/1.0"
    for key, value in settings.items():
        setattr(profile.crawl, key, value)
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({"acquisition_enabled": True, "authorization_reference": "synthetic"})
    )
    try:
        if expected is None:
            with pytest.raises(ValueError, match="outside allowed domains"):
                acquire(profile, tmp_path / "sources", policy, allow_loopback=True)
            assert not observed_agents
        else:
            report = acquire(profile, tmp_path / "sources", policy, allow_loopback=True)
            assert report["documents"] == expected
            assert all(agent == "ChatSparkSyntheticTest/1.0" for agent in observed_agents)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "delay,target,expected", [(3, 1, [3, 6, 9]), (0, 0.5, [1, 3, 5]), (0, 2, [1, 2, 3])]
)
def test_crawl_delay_and_adaptive_target_change_request_schedule(
    delay, target, expected, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import chatspark.ingestion.acquire as module

    now = 0.0
    starts = []

    def sleep(seconds):
        nonlocal now
        now += seconds

    class Pool:
        def __init__(self, **options):
            pass

        def urlopen(self, method, path, **options):
            nonlocal now
            if path != "/robots.txt":
                starts.append(now)
            now += 1
            body = (
                b"User-agent: *\nAllow: /\n"
                if path == "/robots.txt"
                else b"<main><h1>Workshop</h1><p>Authored synthetic registration guide.</p></main>"
            )
            parts = iter([body, b""])
            return SimpleNamespace(
                status=200,
                headers={"Content-Type": "text/plain" if path == "/robots.txt" else "text/html"},
                connection=None,
                read=lambda size: next(parts),
                close=lambda: None,
            )

        def close(self):
            pass

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
    monkeypatch.setattr(module.urllib3, "HTTPConnectionPool", Pool)
    profile = load_profile("example")
    profile.crawl.allowed_domains = ["127.0.0.1"]
    profile.crawl.start_urls = [f"http://127.0.0.1/{i}" for i in range(3)]
    profile.crawl.concurrent_requests_per_domain = 1
    profile.crawl.download_delay_seconds = delay
    profile.crawl.autothrottle_target_concurrency = target
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({"acquisition_enabled": True, "authorization_reference": "synthetic"})
    )
    assert acquire(profile, tmp_path / "sources", policy, allow_loopback=True)["documents"] == 3
    assert starts == expected


def test_shared_retrieval_budgets_and_weights_change_stages(candidate):
    from chatspark.plugins import ProviderDescriptor, RankedEvidence, Registry
    from chatspark.profiles.models import HybridSettings
    from chatspark.retrieval.pipeline import HybridRetriever, execution_context
    from chatspark.retrieval.sparse_sqlite import SparseOptions
    from chatspark.storage.evidence import EvidenceStore

    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    rows = EvidenceStore(database).rows(
        corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id
    )
    ids = [row["chunk_id"] for row in rows]
    calls = []

    class Lane:
        def __init__(self, name, order):
            self.name, self.order = name, order

        def retrieve(self, query, limit, context):
            calls.append((self.name, limit, context.requested_limit))
            return [RankedEvidence(identifier, 1) for identifier in self.order[:limit]]

    registry = Registry()
    registry.register(
        ProviderDescriptor(
            "sparse",
            "test",
            frozenset({"retrieval"}),
            SparseOptions,
            lambda options: Lane("sparse", ids),
        )
    )
    profile = load_profile("example").retrieval
    profile.enable_dense = True
    profile.dense_k, profile.sparse_k = 1, 2
    profile.rrf_dense_weight, profile.rrf_sparse_weight = 10, 1
    profile.rrf_k = 20
    profile.rerank_input_k, profile.final_k = 1, 1
    retriever = HybridRetriever(
        context=context,
        profile=profile,
        registry=registry,
        dense=Lane("dense", ids[::-1]),
        options=HybridSettings(),
    )
    result = retriever.retrieve_result("registration", 8)
    assert calls == [("sparse", 2, 2), ("dense", 1, 1)]
    assert result.items[0].chunk_id == ids[-1]
    assert result.items[0].score == pytest.approx(10 / 21 + 1 / 22)
    assert len(result.stages["reranked"]) == len(result.items) == 1
    profile.rrf_dense_weight, profile.rrf_sparse_weight = 0, 1
    profile.rrf_k = 60
    result = retriever.retrieve_result("registration", 8)
    assert result.items[0].chunk_id == ids[0]
    assert result.items[0].score == pytest.approx(1 / 61)


def test_fts_field_weights_change_the_canonical_winner(candidate):
    import sqlite3

    from chatspark.retrieval.pipeline import execution_context
    from chatspark.retrieval.sparse_sqlite import SparseOptions, V3SQLiteSparseRetriever

    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    with sqlite3.connect(database) as con:
        ids = [
            row[0] for row in con.execute("SELECT chunk_id FROM v3_chunks_fts ORDER BY chunk_id")
        ]
        con.execute(
            "UPDATE v3_chunks_fts SET content='orchidmarker', display_title='', heading_text='', url_tokens='' WHERE chunk_id=?",
            (ids[0],),
        )
        con.execute(
            "UPDATE v3_chunks_fts SET content='', display_title='orchidmarker', heading_text='', url_tokens='' WHERE chunk_id=?",
            (ids[1],),
        )
        con.commit()
    # FTS indexing precedes the immutable query snapshot.
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    text = V3SQLiteSparseRetriever(SparseOptions(fts_bm25_weights=[10, 1, 0, 0]))
    title = V3SQLiteSparseRetriever(SparseOptions(fts_bm25_weights=[1, 10, 0, 0]))
    assert text.retrieve('orchidmarker " OR *', 1, context)[0].evidence_id == ids[0]
    assert title.retrieve('orchidmarker " OR *', 1, context)[0].evidence_id == ids[1]


@pytest.mark.parametrize(
    "scope,minimum,count", [("all", 0, 2), ("html-only", 0, 1), ("all", 1000, 0)]
)
def test_local_build_scope_and_minimum_text_filter_consistently(tmp_path, scope, minimum, count):
    import sqlite3

    source = tmp_path / "source"
    source.mkdir()
    (source / "guide.html").write_text(
        "<main><h1>Workshop</h1><p>Authored registration guide ORCHID-42.</p></main>"
    )
    (source / "guide.md").write_text("# Workshop\n\nAuthored registration guide ORCHID-42.\n")
    profile = load_profile("example")
    profile.extraction.content_scope = scope
    profile.extraction.min_document_text_chars = minimum
    path = tmp_path / "profile.json"
    path.write_text(profile.model_dump_json())
    result = execute_build(
        EngineBuildRequestV1(
            build_id="filter",
            source_root=source,
            output_root=tmp_path / "candidate",
            profile=str(path),
        )
    )
    assert result.status == "succeeded", result.error
    with sqlite3.connect(tmp_path / "candidate/corpus.db") as con:
        assert con.execute("SELECT count(*) FROM documents").fetchone()[0] == count
