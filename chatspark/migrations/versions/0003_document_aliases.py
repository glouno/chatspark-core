"""map source URLs to content-canonical documents

Revision ID: 0003_document_aliases
Revises: 0002_incremental_source_observations
"""

from alembic import op

revision = "0003_document_aliases"
down_revision = "0002_incremental_source_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE document_aliases (
            corpus_id TEXT NOT NULL,
            canonical_uri TEXT NOT NULL,
            document_id TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
            deleted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(corpus_id, canonical_uri),
            UNIQUE(source_object_id),
            FOREIGN KEY(corpus_id) REFERENCES corpora(corpus_id),
            FOREIGN KEY(document_id) REFERENCES documents(document_id),
            FOREIGN KEY(source_object_id) REFERENCES source_objects(source_object_id)
        )"""
    )
    op.execute("CREATE INDEX ix_document_aliases_document ON document_aliases(document_id)")
    op.execute(
        """INSERT INTO document_aliases(
               corpus_id, canonical_uri, document_id, source_object_id,
               is_primary, deleted_at, created_at, updated_at
           )
           SELECT corpus_id, canonical_uri, document_id, source_object_id,
                  1, deleted_at, created_at, updated_at FROM documents"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS document_aliases")
