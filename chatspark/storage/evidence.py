import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from chatspark.plugins.contracts import RankedEvidence
from chatspark.retrieval.models import RetrievedChunk


class EvidenceError(ValueError):
    pass


def _snapshot_signature(database):
    stat = database.stat()
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise EvidenceError("Canonical snapshots must be checkpointed before serving")
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=16)
def _snapshot_digest(database, signature):
    with database.open("rb") as stream:
        result = hashlib.file_digest(stream, "sha256").hexdigest()
    if _snapshot_signature(database) != signature:
        raise EvidenceError("Canonical snapshot changed while verifying its digest")
    return result


def snapshot_digest(database):
    database = Path(database).resolve()
    signature = _snapshot_signature(database)
    result = _snapshot_digest(database, signature)
    if _snapshot_signature(database) != signature:
        raise EvidenceError("Canonical snapshot changed while verifying its digest")
    return result


def matches_filters(metadata, filters):
    for key, expected in filters.items():
        actual = metadata.get(key)
        if isinstance(expected, (tuple, list)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def narrow_filters(authorized, proposed):
    for filters in (authorized, proposed):
        if not isinstance(filters, Mapping):
            raise EvidenceError("Evidence filters must be a mapping")
        for key, value in filters.items():
            values = value if isinstance(value, (list, tuple)) else (value,)
            if (
                not isinstance(key, str)
                or not key
                or any(
                    type(item) not in (str, int, float, bool, type(None))
                    or (isinstance(item, float) and not math.isfinite(item))
                    for item in values
                )
            ):
                raise EvidenceError("Evidence filters require named scalar values")
    result = dict(authorized)
    for key, value in proposed.items():
        if key in result:
            old = result[key]
            old_values = set(old) if isinstance(old, (list, tuple)) else {old}
            new_values = set(value) if isinstance(value, (list, tuple)) else {value}
            if not new_values <= old_values:
                raise EvidenceError("Provider attempted to widen authorization")
        result[key] = value
    return result


class EvidenceStore:
    def __init__(self, database):
        self.database = Path(database).resolve()

    def _connect(self):
        con = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def rows(self, *, corpus_id, chunk_set_id, evidence_ids=None, section_ids=None):
        clauses = []
        parameters = []
        source = "chunks c"
        if evidence_ids is not None:
            # Drive the join from the small requested ID set. An IN subquery
            # lets SQLite choose a full chunk-set scan despite the unique ID index.
            ids = list(dict.fromkeys(evidence_ids))
            if not ids:
                return []
            source = "json_each(?) ids CROSS JOIN chunks c ON c.chunk_id=ids.value"
            parameters.append(json.dumps(ids))
        parameters.extend([corpus_id, chunk_set_id])
        if section_ids is not None:
            clauses.append("c.section_id IN (SELECT value FROM json_each(?))")
            parameters.append(json.dumps(list(section_ids)))
        selection = " AND " + " AND ".join(clauses) if clauses else ""
        with self._connect() as con:
            return con.execute(
                """SELECT c.chunk_id, c.text, c.metadata_json, c.chunk_set_id,
                d.document_id, d.corpus_id, d.canonical_uri, r.title, r.source_modified_at,
                r.media_type, s.heading_path_json, c.section_id, c.ordinal, cs.chunker, co.name AS corpus_name
                FROM """
                + source
                + """ JOIN document_revisions r ON r.revision_id=c.revision_id
                JOIN documents d ON d.document_id=r.document_id
                LEFT JOIN sections s ON s.section_id=c.section_id
                JOIN chunk_sets cs ON cs.chunk_set_id=c.chunk_set_id
                JOIN corpora co ON co.corpus_id=d.corpus_id
                WHERE d.corpus_id=? AND c.chunk_set_id=? AND d.deleted_at IS NULL
                AND d.current_revision_id=c.revision_id"""
                + selection
                + " ORDER BY c.chunk_id",
                parameters,
            ).fetchall()

    @staticmethod
    def chunk(row, score=0, source="canonical"):
        meta = json.loads(row["metadata_json"] or "{}")
        meta.update(
            {
                "chunk_id": row["chunk_id"],
                "doc_id": row["document_id"],
                "corpus_id": row["corpus_id"],
                "chunk_set_id": row["chunk_set_id"],
                "url": row["canonical_uri"],
                "title": row["title"],
                "heading_path": json.loads(row["heading_path_json"] or "[]"),
                "published_at": row["source_modified_at"],
                "section_id": row["section_id"],
                "chunk_index": row["ordinal"],
                "chunk_strategy": row["chunker"],
                "corpus_name": row["corpus_name"],
                "retrieval_corpus_id": row["corpus_name"],
                "content_type": row["media_type"],
            }
        )
        return RetrievedChunk(row["chunk_id"], row["text"], meta, score, source)

    def _validate_candidates(self, candidates, maximum):
        if len(candidates) > maximum:
            raise EvidenceError("Provider exceeded result budget")
        for candidate in candidates:
            if not isinstance(candidate, RankedEvidence) or not isinstance(
                candidate.evidence_id, str
            ):
                raise EvidenceError("Provider returned an invalid evidence record")

    def _materialize(self, candidates, rows, context, allowed_ids=None):
        result = []
        seen = set()
        for candidate in candidates:
            if candidate.evidence_id not in rows or (
                allowed_ids is not None and candidate.evidence_id not in allowed_ids
            ):
                raise EvidenceError("Provider returned unknown, stale or unauthorized evidence")
            # New objects per stage: callbacks must never mutate another stage's
            # canonical text, metadata or scores through shared object references.
            chunk = self.chunk(rows[candidate.evidence_id], candidate.score)
            if not matches_filters(chunk.metadata, context.filters):
                raise EvidenceError("Provider returned unauthorized evidence")
            if chunk.chunk_id not in seen:
                result.append(chunk)
                seen.add(chunk.chunk_id)
        return result

    def resolve_groups(self, groups, context, *, maximum=1000):
        """Resolve bounded ranking stages in one read, with per-stage validation.

        Reuse is limited to this call on one immutable snapshot and filter scope.
        Final/context/generation boundaries perform independent validation.
        """
        if len(groups) > 33:
            raise EvidenceError("Provider exceeded ranking stage budget")
        if snapshot_digest(self.database) != context.snapshot_sha256:
            raise EvidenceError("Canonical snapshot changed during retrieval")
        ids = set()
        for candidates in groups.values():
            self._validate_candidates(candidates, maximum)
            ids.update(c.evidence_id for c in candidates)
        rows = {
            row["chunk_id"]: row
            for row in self.rows(
                corpus_id=context.corpus_id, chunk_set_id=context.chunk_set_id, evidence_ids=ids
            )
        }
        if snapshot_digest(self.database) != context.snapshot_sha256:
            raise EvidenceError("Canonical snapshot changed during retrieval")
        return {
            name: self._materialize(candidates, rows, context)
            for name, candidates in groups.items()
        }

    def resolve(self, candidates, context, *, maximum=1000, allowed_ids=None):
        self._validate_candidates(candidates, maximum)
        if snapshot_digest(self.database) != context.snapshot_sha256:
            raise EvidenceError("Canonical snapshot changed during retrieval")
        rows = {
            row["chunk_id"]: row
            for row in self.rows(
                corpus_id=context.corpus_id,
                chunk_set_id=context.chunk_set_id,
                evidence_ids=[candidate.evidence_id for candidate in candidates],
            )
        }
        if snapshot_digest(self.database) != context.snapshot_sha256:
            raise EvidenceError("Canonical snapshot changed during retrieval")
        return self._materialize(candidates, rows, context, allowed_ids)
