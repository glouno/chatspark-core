"""record incremental source observations

Revision ID: 0002_incremental_source_observations
Revises: 0001_chatspark_schema_v3
"""

from alembic import op

revision = "0002_incremental_source_observations"
down_revision = "0001_chatspark_schema_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE source_observations (
            observation_id TEXT PRIMARY KEY,
            ingestion_run_id TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            raw_path TEXT,
            raw_sha256 TEXT,
            canonical_content_hash TEXT,
            status TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(ingestion_run_id, source_object_id),
            FOREIGN KEY(ingestion_run_id) REFERENCES ingestion_runs(ingestion_run_id),
            FOREIGN KEY(source_object_id) REFERENCES source_objects(source_object_id),
            CHECK(status IN ('added', 'content_changed', 'metadata_only', 'unchanged', 'deleted'))
        )"""
    )
    op.execute(
        "CREATE INDEX ix_source_observations_source ON source_observations(source_object_id)"
    )
    op.execute("CREATE INDEX ix_source_observations_raw_hash ON source_observations(raw_sha256)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS source_observations")
