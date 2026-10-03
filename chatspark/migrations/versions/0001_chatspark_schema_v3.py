"""chatspark canonical schema v3

Revision ID: 0001_chatspark_schema_v3
Revises: None
Create Date: 2026-06-21
"""

from alembic import op

revision = "0001_chatspark_schema_v3"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = [
        """CREATE TABLE schema_metadata (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE corpora (
            corpus_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            metadata_json JSON, created_at TEXT NOT NULL,
            CHECK (metadata_json IS NULL OR json_valid(metadata_json))
        )""",
        """CREATE TABLE profile_snapshots (
            profile_digest TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL, profile_yaml TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE source_systems (
            source_system_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL,
            kind TEXT NOT NULL, root_uri TEXT NOT NULL, owner TEXT,
            access_policy_json JSON, metadata_json JSON, created_at TEXT NOT NULL,
            UNIQUE(corpus_id, root_uri),
            FOREIGN KEY(corpus_id) REFERENCES corpora(corpus_id),
            CHECK (access_policy_json IS NULL OR json_valid(access_policy_json)),
            CHECK (metadata_json IS NULL OR json_valid(metadata_json))
        )""",
        """CREATE TABLE ingestion_runs (
            ingestion_run_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL,
            profile_digest TEXT NOT NULL, source_snapshot TEXT,
            code_version TEXT, provenance_quality TEXT NOT NULL,
            status TEXT NOT NULL, config_json JSON, stats_json JSON,
            started_at TEXT NOT NULL, finished_at TEXT,
            FOREIGN KEY(corpus_id) REFERENCES corpora(corpus_id),
            FOREIGN KEY(profile_digest) REFERENCES profile_snapshots(profile_digest),
            CHECK (provenance_quality IN ('recorded', 'inferred')),
            CHECK (config_json IS NULL OR json_valid(config_json)),
            CHECK (stats_json IS NULL OR json_valid(stats_json))
        )""",
        """CREATE TABLE source_objects (
            source_object_id TEXT PRIMARY KEY, source_system_id TEXT NOT NULL,
            canonical_uri TEXT NOT NULL, media_type TEXT, content_hash TEXT,
            etag TEXT, source_modified_at TEXT, fetched_at TEXT,
            http_status INTEGER, deleted_at TEXT, metadata_json JSON,
            UNIQUE(source_system_id, canonical_uri),
            FOREIGN KEY(source_system_id) REFERENCES source_systems(source_system_id),
            CHECK (metadata_json IS NULL OR json_valid(metadata_json))
        )""",
        """CREATE TABLE documents (
            document_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL,
            source_object_id TEXT NOT NULL, canonical_uri TEXT NOT NULL,
            current_revision_id TEXT, authority TEXT, deleted_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(corpus_id, canonical_uri),
            FOREIGN KEY(corpus_id) REFERENCES corpora(corpus_id),
            FOREIGN KEY(source_object_id) REFERENCES source_objects(source_object_id),
            FOREIGN KEY(current_revision_id) REFERENCES document_revisions(revision_id)
        )""",
        """CREATE TABLE document_revisions (
            revision_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
            ingestion_run_id TEXT NOT NULL, content_hash TEXT NOT NULL,
            media_type TEXT, language TEXT, title TEXT, text TEXT NOT NULL,
            markdown TEXT, source_modified_at TEXT, fetched_at TEXT,
            provenance_json JSON NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(document_id, content_hash),
            FOREIGN KEY(document_id) REFERENCES documents(document_id),
            FOREIGN KEY(ingestion_run_id) REFERENCES ingestion_runs(ingestion_run_id),
            CHECK (json_valid(provenance_json))
        )""",
        """CREATE TABLE sections (
            section_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL,
            parent_section_id TEXT, ordinal INTEGER NOT NULL,
            heading_path_json JSON, text TEXT NOT NULL,
            start_offset INTEGER, end_offset INTEGER, token_count INTEGER,
            UNIQUE(revision_id, ordinal),
            FOREIGN KEY(revision_id) REFERENCES document_revisions(revision_id),
            FOREIGN KEY(parent_section_id) REFERENCES sections(section_id),
            CHECK (heading_path_json IS NULL OR json_valid(heading_path_json)),
            CHECK (start_offset IS NULL OR end_offset IS NULL OR start_offset <= end_offset)
        )""",
        """CREATE TABLE chunk_sets (
            chunk_set_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL,
            ingestion_run_id TEXT NOT NULL, profile_digest TEXT NOT NULL,
            chunker TEXT NOT NULL, provenance_quality TEXT NOT NULL,
            config_json JSON NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(corpus_id) REFERENCES corpora(corpus_id),
            FOREIGN KEY(ingestion_run_id) REFERENCES ingestion_runs(ingestion_run_id),
            FOREIGN KEY(profile_digest) REFERENCES profile_snapshots(profile_digest),
            CHECK (provenance_quality IN ('recorded', 'inferred')),
            CHECK (json_valid(config_json))
        )""",
        """CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY, chunk_set_id TEXT NOT NULL,
            revision_id TEXT NOT NULL, section_id TEXT, ordinal INTEGER NOT NULL,
            text TEXT NOT NULL, token_count INTEGER, metadata_json JSON,
            UNIQUE(chunk_set_id, revision_id, ordinal),
            FOREIGN KEY(chunk_set_id) REFERENCES chunk_sets(chunk_set_id),
            FOREIGN KEY(revision_id) REFERENCES document_revisions(revision_id),
            FOREIGN KEY(section_id) REFERENCES sections(section_id),
            CHECK (metadata_json IS NULL OR json_valid(metadata_json))
        )""",
        """CREATE TABLE index_runs (
            index_run_id TEXT PRIMARY KEY, chunk_set_id TEXT NOT NULL,
            profile_digest TEXT NOT NULL, index_kind TEXT NOT NULL,
            model_id TEXT, model_revision TEXT, target_uri TEXT,
            config_json JSON NOT NULL, status TEXT NOT NULL, stats_json JSON,
            started_at TEXT NOT NULL, finished_at TEXT,
            FOREIGN KEY(chunk_set_id) REFERENCES chunk_sets(chunk_set_id),
            FOREIGN KEY(profile_digest) REFERENCES profile_snapshots(profile_digest),
            CHECK (json_valid(config_json)),
            CHECK (stats_json IS NULL OR json_valid(stats_json))
        )""",
    ]
    for statement in statements:
        op.execute(statement)
    op.execute(
        "INSERT INTO schema_metadata(key, value, updated_at) "
        "VALUES ('app_schema_version', '3', CURRENT_TIMESTAMP)"
    )
    for statement in [
        "CREATE INDEX ix_source_objects_hash ON source_objects(content_hash)",
        "CREATE INDEX ix_document_revisions_document ON document_revisions(document_id)",
        "CREATE INDEX ix_sections_revision ON sections(revision_id)",
        "CREATE INDEX ix_chunks_chunk_set ON chunks(chunk_set_id)",
        "CREATE INDEX ix_chunks_revision ON chunks(revision_id)",
        "CREATE INDEX ix_index_runs_chunk_set ON index_runs(chunk_set_id)",
    ]:
        op.execute(statement)


def downgrade() -> None:
    for table in [
        "index_runs",
        "chunks",
        "chunk_sets",
        "sections",
        "document_revisions",
        "documents",
        "source_objects",
        "ingestion_runs",
        "source_systems",
        "profile_snapshots",
        "corpora",
        "schema_metadata",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table}")
