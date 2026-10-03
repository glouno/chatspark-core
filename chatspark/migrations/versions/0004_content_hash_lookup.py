"""index canonical content lookup

Revision ID: 0004_content_hash_lookup
Revises: 0003_document_aliases
"""

from alembic import op

revision = "0004_content_hash_lookup"
down_revision = "0003_document_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_document_revisions_content_hash ON document_revisions(content_hash)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_revisions_content_hash")
