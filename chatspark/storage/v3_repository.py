from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from chatspark.runtime.config import Settings
from chatspark.storage.migrations import initialize_v3_database


class V3Repository:
    """Transactional repository for an explicitly selected canonical v3 DB."""

    def __init__(self, database: str | Path):
        self.path = Path(database).expanduser().resolve()
        initialize_v3_database(self.path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._identity_seed = _load_identity_seed(
            os.environ.get("CHATSPARK_IDENTITY_BASELINE_DATABASE")
        )

    def identity_sort_key(
        self, corpus_id: str, canonical_uri: str
    ) -> tuple[int, tuple[int, int, str]]:
        seed = self._identity_seed.get((corpus_id, canonical_uri))
        phase = 0 if seed and seed["is_owner"] else 1 if seed else 2
        return (phase, _canonical_uri_rank(canonical_uri))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> V3Repository:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[V3Repository]:
        try:
            self.connection.execute("BEGIN")
            yield self
            violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ValueError(f"V3 transaction created {len(violations)} foreign-key violations")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def ensure_corpus(self, *, name: str, metadata: dict[str, Any] | None = None) -> str:
        corpus_id = stable_id("corpus", name)
        self.connection.execute(
            "INSERT INTO corpora(corpus_id, name, metadata_json, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET metadata_json = COALESCE(excluded.metadata_json, corpora.metadata_json)",
            (corpus_id, name, _json(metadata), utc_now()),
        )
        return corpus_id

    def ensure_profile(
        self,
        *,
        digest: str,
        profile_id: str,
        schema_version: int,
        profile_yaml: str,
    ) -> str:
        self.connection.execute(
            "INSERT OR IGNORE INTO profile_snapshots VALUES (?, ?, ?, ?, ?)",
            (digest, profile_id, schema_version, profile_yaml, utc_now()),
        )
        return digest

    def ensure_source_system(
        self,
        *,
        corpus_id: str,
        kind: str,
        root_uri: str,
        owner: str | None = None,
        access_policy: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        source_system_id = stable_id("source-system", corpus_id, root_uri)
        self.connection.execute(
            "INSERT INTO source_systems VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(corpus_id, root_uri) DO UPDATE SET owner = excluded.owner, "
            "access_policy_json = excluded.access_policy_json, metadata_json = excluded.metadata_json",
            (
                source_system_id,
                corpus_id,
                kind,
                root_uri,
                owner,
                _json(access_policy),
                _json(metadata),
                utc_now(),
            ),
        )
        return source_system_id

    def start_ingestion_run(
        self,
        *,
        corpus_id: str,
        profile_digest: str,
        source_snapshot: str | None,
        code_version: str | None,
        provenance_quality: str = "recorded",
        config: dict[str, Any] | None = None,
        ingestion_run_id: str | None = None,
    ) -> str:
        run_id = ingestion_run_id or uuid.uuid4().hex
        self.connection.execute(
            "INSERT INTO ingestion_runs VALUES (?, ?, ?, ?, ?, ?, 'running', ?, NULL, ?, NULL)",
            (
                run_id,
                corpus_id,
                profile_digest,
                source_snapshot,
                code_version,
                provenance_quality,
                _json(config),
                utc_now(),
            ),
        )
        return run_id

    def finish_ingestion_run(
        self,
        ingestion_run_id: str,
        *,
        status: str,
        stats: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            "UPDATE ingestion_runs SET status = ?, stats_json = ?, finished_at = ? WHERE ingestion_run_id = ?",
            (status, _json(stats), utc_now(), ingestion_run_id),
        )

    def record_source_observation(
        self,
        *,
        ingestion_run_id: str,
        source_system_id: str,
        canonical_uri: str,
        status: str,
        raw_path: str | None = None,
        raw_sha256: str | None = None,
        canonical_content_hash: str | None = None,
    ) -> str:
        source_object_id = stable_id("source-object", source_system_id, canonical_uri)
        observation_id = stable_id("source-observation", ingestion_run_id, source_object_id)
        self.connection.execute(
            "INSERT INTO source_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ingestion_run_id, source_object_id) DO UPDATE SET "
            "raw_path=excluded.raw_path, raw_sha256=excluded.raw_sha256, "
            "canonical_content_hash=excluded.canonical_content_hash, status=excluded.status, "
            "observed_at=excluded.observed_at",
            (
                observation_id,
                ingestion_run_id,
                source_object_id,
                raw_path,
                raw_sha256,
                canonical_content_hash,
                status,
                utc_now(),
            ),
        )
        return observation_id

    def mark_document_deleted(
        self,
        *,
        source_system_id: str,
        canonical_uri: str,
    ) -> str:
        source_object_id = stable_id("source-object", source_system_id, canonical_uri)
        now = utc_now()
        cursor = self.connection.execute(
            "UPDATE source_objects SET deleted_at = ?, fetched_at = ? "
            "WHERE source_object_id = ? AND deleted_at IS NULL",
            (now, now, source_object_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"Cannot delete unknown or already deleted source object: {canonical_uri}"
            )
        alias = self.connection.execute(
            "SELECT document_id FROM document_aliases WHERE source_object_id = ? AND deleted_at IS NULL",
            (source_object_id,),
        ).fetchone()
        if alias:
            document_id = str(alias["document_id"])
            self.connection.execute(
                "UPDATE document_aliases SET deleted_at = ?, is_primary = 0, updated_at = ? WHERE source_object_id = ?",
                (now, now, source_object_id),
            )
            remaining = self.connection.execute(
                """SELECT canonical_uri, source_object_id FROM document_aliases
                   WHERE document_id = ? AND deleted_at IS NULL""",
                (document_id,),
            ).fetchall()
            if remaining:
                primary = min(
                    remaining, key=lambda item: _canonical_uri_rank(str(item["canonical_uri"]))
                )
                self.connection.execute(
                    "UPDATE document_aliases SET is_primary = (canonical_uri = ?), updated_at = ? WHERE document_id = ?",
                    (str(primary["canonical_uri"]), now, document_id),
                )
                self.connection.execute(
                    """UPDATE documents SET canonical_uri = ?, source_object_id = ?,
                       deleted_at = NULL, updated_at = ? WHERE document_id = ?""",
                    (
                        str(primary["canonical_uri"]),
                        str(primary["source_object_id"]),
                        now,
                        document_id,
                    ),
                )
            else:
                self.connection.execute(
                    "UPDATE documents SET deleted_at = ?, updated_at = ? WHERE document_id = ?",
                    (now, now, document_id),
                )
        else:
            self.connection.execute(
                "UPDATE documents SET deleted_at = ?, updated_at = ? WHERE source_object_id = ?",
                (now, now, source_object_id),
            )
        return source_object_id

    def upsert_document_revision(
        self,
        *,
        corpus_id: str,
        source_system_id: str,
        ingestion_run_id: str,
        document_id: str,
        canonical_uri: str,
        content_hash: str,
        text: str,
        markdown: str = "",
        media_type: str | None = None,
        language: str | None = None,
        title: str | None = None,
        fetched_at: str | None = None,
        source_modified_at: str | None = None,
        etag: str | None = None,
        http_status: int | None = None,
        authority: str | None = None,
        provenance: dict[str, Any] | None = None,
        source_metadata: dict[str, Any] | None = None,
        deleted: bool = False,
    ) -> tuple[str, bool]:
        source_object_id = stable_id("source-object", source_system_id, canonical_uri)
        revision_id = stable_id("revision", document_id, content_hash)
        now = utc_now()
        deleted_at = now if deleted else None
        self.connection.execute(
            "INSERT INTO source_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_system_id, canonical_uri) DO UPDATE SET "
            "media_type=excluded.media_type, content_hash=excluded.content_hash, etag=excluded.etag, "
            "source_modified_at=excluded.source_modified_at, fetched_at=excluded.fetched_at, "
            "http_status=excluded.http_status, deleted_at=excluded.deleted_at, metadata_json=excluded.metadata_json",
            (
                source_object_id,
                source_system_id,
                canonical_uri,
                media_type,
                content_hash,
                etag,
                source_modified_at,
                fetched_at,
                http_status,
                deleted_at,
                _json(source_metadata),
            ),
        )
        self.connection.execute(
            "INSERT INTO documents(document_id, corpus_id, source_object_id, canonical_uri, current_revision_id, "
            "authority, deleted_at, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?) "
            "ON CONFLICT(document_id) DO UPDATE SET source_object_id=excluded.source_object_id, "
            "canonical_uri=excluded.canonical_uri, authority=excluded.authority, "
            "deleted_at=excluded.deleted_at, updated_at=excluded.updated_at",
            (
                document_id,
                corpus_id,
                source_object_id,
                canonical_uri,
                authority,
                deleted_at,
                now,
                now,
            ),
        )
        existing = self.connection.execute(
            "SELECT 1 FROM document_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        created = existing is None
        if created:
            self.connection.execute(
                "INSERT INTO document_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    document_id,
                    ingestion_run_id,
                    content_hash,
                    media_type,
                    language,
                    title,
                    text,
                    markdown,
                    source_modified_at,
                    fetched_at,
                    _json(provenance or {}),
                    now,
                ),
            )
        self.connection.execute(
            "UPDATE documents SET current_revision_id = ?, deleted_at = ?, updated_at = ? WHERE document_id = ?",
            (revision_id, deleted_at, now, document_id),
        )
        return revision_id, created

    def upsert_content_document_revision(self, **kwargs: Any) -> tuple[str, bool]:
        """Store one document per content hash while retaining every source URL."""
        corpus_id = str(kwargs["corpus_id"])
        content_hash = str(kwargs["content_hash"])
        canonical_uri = str(kwargs["canonical_uri"])
        requested_document_id = str(kwargs["document_id"])
        source_system_id = str(kwargs["source_system_id"])
        source_object_id = stable_id("source-object", source_system_id, canonical_uri)
        # Empty extraction is not evidence that unrelated URLs are aliases.
        # Treat it as a source-specific document, preserving its URL identity.
        dedup_eligible = bool(str(kwargs.get("text") or "").strip())
        seed = self._identity_seed.get((corpus_id, canonical_uri))
        existing = (
            self.connection.execute(
                """SELECT d.document_id FROM documents d
               JOIN document_revisions r ON r.revision_id = d.current_revision_id
               WHERE d.corpus_id = ? AND d.deleted_at IS NULL AND r.content_hash = ?
               ORDER BY d.document_id LIMIT 1""",
                (corpus_id, content_hash),
            ).fetchone()
            if dedup_eligible
            else None
        )
        alias_owner = self.connection.execute(
            "SELECT document_id FROM document_aliases WHERE corpus_id = ? AND canonical_uri = ?",
            (corpus_id, canonical_uri),
        ).fetchone()
        owner_id = str(alias_owner["document_id"]) if alias_owner else None
        owner_aliases = (
            self.connection.execute(
                "SELECT COUNT(*) FROM document_aliases WHERE document_id = ? AND deleted_at IS NULL",
                (owner_id,),
            ).fetchone()[0]
            if owner_id
            else 0
        )
        owner_hash = None
        owner_source_id = None
        if owner_id:
            owner_revision = self.connection.execute(
                """SELECT r.content_hash, d.owner_source_object_id FROM documents d
                   JOIN document_revisions r ON r.revision_id = d.current_revision_id
                   WHERE d.document_id = ?""",
                (owner_id,),
            ).fetchone()
            owner_hash = str(owner_revision["content_hash"]) if owner_revision else None
            owner_source_id = (
                str(owner_revision["owner_source_object_id"]) if owner_revision else None
            )
        seeded_id = None
        if seed and (seed["content_hash"] == content_hash or seed["is_owner"]):
            seeded_id = str(seed["document_id"])
            seeded_current = self.connection.execute(
                """SELECT r.content_hash FROM documents d JOIN document_revisions r
                   ON r.revision_id = d.current_revision_id WHERE d.document_id = ?""",
                (seeded_id,),
            ).fetchone()
            if (
                seeded_current
                and str(seeded_current["content_hash"]) != content_hash
                and not seed["is_owner"]
            ):
                seeded_id = None
        if not dedup_eligible:
            document_id = (
                owner_id
                if owner_id and (owner_aliases == 1 or owner_source_id == source_object_id)
                else (seeded_id or requested_document_id)
            )
        elif owner_id and (owner_aliases == 1 or owner_hash == content_hash):
            # A URL that owns its document retains that identity across revisions.
            # An unchanged member of a shared document also retains membership.
            document_id = owner_id
        elif seeded_id:
            document_id = seeded_id
        elif existing:
            document_id = str(existing["document_id"])
        else:
            # Split one diverging alias away from other URLs that retain old content.
            document_id = (
                requested_document_id
                if not owner_id
                else stable_id("content-document", corpus_id, content_hash)
            )
        kwargs["document_id"] = document_id
        revision_id, created = self.upsert_document_revision(**kwargs)
        self.connection.execute(
            """UPDATE documents SET owner_source_object_id =
               COALESCE(owner_source_object_id, ?), owner_canonical_uri =
               COALESCE(owner_canonical_uri, ?) WHERE document_id = ?""",
            (
                str(seed["owner_source_object_id"]) if seeded_id and seed else source_object_id,
                str(seed["owner_canonical_uri"]) if seeded_id and seed else canonical_uri,
                document_id,
            ),
        )
        now = utc_now()
        self.connection.execute(
            """INSERT INTO document_aliases(
                   corpus_id, canonical_uri, document_id, source_object_id,
                   is_primary, deleted_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 0, NULL, ?, ?)
               ON CONFLICT(corpus_id, canonical_uri) DO UPDATE SET
                   document_id=excluded.document_id, source_object_id=excluded.source_object_id,
                   deleted_at=NULL, updated_at=excluded.updated_at""",
            (corpus_id, canonical_uri, document_id, source_object_id, now, now),
        )
        aliases = self.connection.execute(
            """SELECT canonical_uri, source_object_id FROM document_aliases
               WHERE document_id = ? AND deleted_at IS NULL""",
            (document_id,),
        ).fetchall()
        primary = min(aliases, key=lambda item: _canonical_uri_rank(str(item["canonical_uri"])))
        primary_uri = str(primary["canonical_uri"])
        primary_source = str(primary["source_object_id"])
        self.connection.execute(
            "UPDATE document_aliases SET is_primary = (canonical_uri = ?), updated_at = ? WHERE document_id = ?",
            (primary_uri, now, document_id),
        )
        self.connection.execute(
            "UPDATE documents SET canonical_uri = ?, source_object_id = ?, updated_at = ? WHERE document_id = ?",
            (primary_uri, primary_source, now, document_id),
        )
        return revision_id, created

    def add_section(
        self,
        *,
        revision_id: str,
        ordinal: int,
        text: str,
        heading_path: list[str] | None = None,
        parent_section_id: str | None = None,
        start_offset: int | None = None,
        end_offset: int | None = None,
    ) -> str:
        section_id = stable_id("section", revision_id, str(ordinal))
        self.connection.execute(
            "INSERT OR IGNORE INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                section_id,
                revision_id,
                parent_section_id,
                ordinal,
                _json(heading_path),
                text,
                start_offset,
                end_offset,
                len(text.split()),
            ),
        )
        return section_id

    def create_chunk_set(
        self,
        *,
        corpus_id: str,
        ingestion_run_id: str,
        profile_digest: str,
        chunker: str,
        config: dict[str, Any],
        provenance_quality: str = "recorded",
        chunk_set_id: str | None = None,
    ) -> str:
        identifier = chunk_set_id or stable_id(
            "chunk-set", corpus_id, ingestion_run_id, chunker, json.dumps(config, sort_keys=True)
        )
        self.connection.execute(
            "INSERT INTO chunk_sets VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (
                identifier,
                corpus_id,
                ingestion_run_id,
                profile_digest,
                chunker,
                provenance_quality,
                _json(config),
                utc_now(),
            ),
        )
        return identifier

    def add_chunk(
        self,
        *,
        chunk_set_id: str,
        revision_id: str,
        ordinal: int,
        text: str,
        section_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        chunk_id: str | None = None,
    ) -> str:
        chunk_id = chunk_id or stable_id("chunk", chunk_set_id, revision_id, str(ordinal))
        self.connection.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                chunk_set_id,
                revision_id,
                section_id,
                ordinal,
                text,
                len(text.split()),
                _json(metadata),
            ),
        )
        return chunk_id

    def finish_chunk_set(self, chunk_set_id: str, *, status: str) -> None:
        self.connection.execute(
            "UPDATE chunk_sets SET status = ? WHERE chunk_set_id = ?", (status, chunk_set_id)
        )


def open_configured_v3_repository(cfg: Settings) -> V3Repository:
    if cfg.CHATSPARK_V3_DB_PATH is None:
        raise RuntimeError("CHATSPARK_V3_DB_PATH is required")
    return V3Repository(cfg.CHATSPARK_V3_DB_PATH)


def stable_id(kind: str, *parts: str) -> str:
    payload = chr(31).join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:24]
    return f"rf3_{kind.replace('-', '_')}_{digest}"


def _load_identity_seed(database: str | None) -> dict[tuple[str, str], dict[str, str | bool]]:
    if not database:
        return {}
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Identity baseline database is missing: {path}")
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        has_aliases = (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_aliases'"
            ).fetchone()
            is not None
        )
        columns = {row[1] for row in con.execute("PRAGMA table_info(documents)")}
        owner_source = (
            "COALESCE(d.owner_source_object_id, d.source_object_id)"
            if ("owner_source_object_id" in columns)
            else "d.source_object_id"
        )
        owner_uri = (
            "COALESCE(d.owner_canonical_uri, owner_so.canonical_uri, d.canonical_uri)"
            if ("owner_canonical_uri" in columns)
            else "COALESCE(owner_so.canonical_uri, d.canonical_uri)"
        )
        source = (
            "document_aliases a JOIN documents d ON d.document_id=a.document_id "
            f"LEFT JOIN source_objects owner_so ON owner_so.source_object_id={owner_source} "
            "JOIN document_revisions r ON r.revision_id=d.current_revision_id "
            "WHERE a.deleted_at IS NULL AND d.deleted_at IS NULL"
            if has_aliases
            else "documents d LEFT JOIN source_objects owner_so "
            f"ON owner_so.source_object_id={owner_source} "
            "JOIN document_revisions r ON r.revision_id=d.current_revision_id "
            "WHERE d.deleted_at IS NULL"
        )
        rows = con.execute(
            f"""SELECT d.corpus_id, {"a" if has_aliases else "d"}.canonical_uri AS url,
                       {"a" if has_aliases else "d"}.source_object_id AS source_object_id,
                       d.document_id, r.content_hash, {owner_source} AS owner_source_object_id,
                       {owner_uri} AS owner_canonical_uri
                FROM {source}"""
        ).fetchall()
    legacy_owners: dict[tuple[str, str], sqlite3.Row] = {}
    if not has_aliases:
        for row in rows:
            if str(row["content_hash"]) == hashlib.sha256(b"").hexdigest():
                continue
            key = (str(row["corpus_id"]), str(row["content_hash"]))
            prior = legacy_owners.get(key)
            if prior is None or _canonical_uri_rank(str(row["url"])) < _canonical_uri_rank(
                str(prior["url"])
            ):
                legacy_owners[key] = row
    seeds: dict[tuple[str, str], dict[str, str | bool]] = {}
    for row in rows:
        corpus_id = str(row["corpus_id"])
        owner = legacy_owners.get((corpus_id, str(row["content_hash"])), row)
        owner_source_id = str(owner["owner_source_object_id"])
        owner_canonical_uri = str(owner["owner_canonical_uri"])
        value: dict[str, str | bool] = {
            "document_id": str(owner["document_id"]),
            "content_hash": str(row["content_hash"]),
            "owner_source_object_id": owner_source_id,
            "owner_canonical_uri": owner_canonical_uri,
            "is_owner": str(row["source_object_id"]) == owner_source_id,
        }
        seeds[(corpus_id, str(row["url"]))] = value
        # The owner may have disappeared from this snapshot. Preserve its URL
        # as a seed so a later return retains the original document identity.
        seeds.setdefault(
            (corpus_id, owner_canonical_uri),
            {
                **value,
                "is_owner": True,
            },
        )
    return seeds


def _canonical_uri_rank(uri: str) -> tuple[int, int, str]:
    parsed = urlsplit(uri)
    return (bool(parsed.query), len(uri), uri)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
