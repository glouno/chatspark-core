import os
from dataclasses import replace
from pathlib import Path

import pytest

from chatspark.plugins import RankedEvidence
from chatspark.retrieval.pipeline import execution_context
from chatspark.storage.evidence import EvidenceError, EvidenceStore, snapshot_digest


def test_hydration_reads_only_requested_rows_and_streams_snapshot(candidate, monkeypatch):
    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    store = EvidenceStore(database)
    rows = store.rows(corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id)
    selected = rows[0]["chunk_id"]
    original_rows = store.rows
    selections = []

    def selected_rows(**kwargs):
        selections.append(kwargs["evidence_ids"])
        result = original_rows(**kwargs)
        assert len(result) == 1
        return result

    monkeypatch.setattr(store, "rows", selected_rows)

    def no_full_read(self):
        raise AssertionError("Snapshot verification must stream the database")

    monkeypatch.setattr(Path, "read_bytes", no_full_read)
    assert store.resolve([RankedEvidence(selected, 1)], context)[0].chunk_id == selected
    assert selections == [[selected]]
    with pytest.raises(EvidenceError, match="snapshot changed"):
        store.resolve([RankedEvidence(selected, 1)], replace(context, snapshot_sha256="0" * 64))


def test_filtered_rows_keep_canonical_snapshot_constraints(candidate):
    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    store = EvidenceStore(database)
    rows = store.rows(corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id)
    ids = [r["chunk_id"] for r in rows]
    assert not store.rows(
        corpus_id="another-corpus", chunk_set_id=context.chunk_set_id, evidence_ids=ids
    )
    assert not store.rows(
        corpus_id=context.corpus_id, chunk_set_id="another-snapshot", evidence_ids=ids
    )
    assert not store.rows(
        corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id, evidence_ids=[]
    )
    assert [
        r["chunk_id"]
        for r in store.rows(
            corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id, evidence_ids=[ids[0]]
        )
    ] == [ids[0]]


def test_cached_snapshot_detects_changes_even_with_restored_mtime(candidate):
    database, _ = candidate
    old_digest = snapshot_digest(database)
    stat = database.stat()
    with database.open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        original = stream.read(1)
        stream.seek(-1, os.SEEK_END)
        stream.write(bytes([original[0] ^ 1]))
    os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert snapshot_digest(database) != old_digest


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_active_sqlite_sidecars_cannot_bypass_snapshot_validation(candidate, suffix):
    database, _ = candidate
    snapshot_digest(database)
    Path(str(database) + suffix).write_bytes(b"uncheckpointed state")
    with pytest.raises(EvidenceError, match="checkpointed"):
        snapshot_digest(database)


def test_id_lookup_work_is_bounded_by_selection_not_corpus(candidate, monkeypatch):
    import sqlite3

    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    with sqlite3.connect(database) as con:
        con.execute(
            """WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000)
            INSERT INTO chunks SELECT 'synthetic-'||x, chunk_set_id, revision_id, section_id,
            ordinal+x, text, token_count, metadata_json
            FROM n CROSS JOIN (SELECT * FROM chunks LIMIT 1)"""
        )
    store = EvidenceStore(database)
    original = store._connect
    steps = [0]

    def connect():
        con = original()

        def progress():
            steps[0] += 100
            return int(steps[0] > 5000)

        con.set_progress_handler(progress, 100)
        return con

    monkeypatch.setattr(store, "_connect", connect)
    selected = store.rows(
        corpus_id=context.corpus_id,
        chunk_set_id=context.chunk_set_id,
        evidence_ids=["synthetic-9999"],
    )
    assert [r["chunk_id"] for r in selected] == ["synthetic-9999"]
    assert steps[0] < 5000


def test_id_and_section_selection_intersects_and_deduplicates(candidate):
    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    store = EvidenceStore(database)
    row = store.rows(corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id)[0]
    kw = dict(
        corpus_id=context.corpus_id,
        chunk_set_id=context.chunk_set_id,
        evidence_ids=[row["chunk_id"], row["chunk_id"]],
    )
    assert len(store.rows(**kw)) == 1
    assert not store.rows(**kw, section_ids=[])
    assert not store.rows(**kw, section_ids=["other"])
    if row["section_id"]:
        assert len(store.rows(**kw, section_ids=[row["section_id"]])) == 1


def test_ranking_groups_share_read_but_not_mutable_evidence(candidate, monkeypatch):
    database, build = candidate
    context = execution_context(database, chunk_set_id=build.chunk_set_id)
    store = EvidenceStore(database)
    cid = store.rows(corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id)[0]["chunk_id"]
    original = store.rows
    calls = []

    def rows(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(store, "rows", rows)
    stages = store.resolve_groups(
        {"dense": [RankedEvidence(cid, 1)], "fused": [RankedEvidence(cid, 2)]}, context
    )
    assert len(calls) == 1
    stages["dense"][0].metadata["url"] = "malicious"
    assert stages["fused"][0].metadata["url"] != "malicious"
    assert stages["fused"][0].score == 2
    with pytest.raises(EvidenceError, match="unauthorized"):
        store.resolve_groups(
            {"valid": [RankedEvidence(cid, 1)], "invalid": [RankedEvidence("unknown", 1)]}, context
        )
    with pytest.raises(EvidenceError, match="budget"):
        store.resolve_groups({"dense": [RankedEvidence(cid, 1)] * 2}, context, maximum=1)
    with pytest.raises(EvidenceError, match="unauthorized"):
        store.resolve_groups(
            {"dense": [RankedEvidence(cid, 1)]}, replace(context, filters={"doc_id": "other"})
        )
