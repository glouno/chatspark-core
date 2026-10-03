"""persist the source that originally owned a deduplicated document

Revision ID: 0005_persistent_document_owner
Revises: 0004_content_hash_lookup
"""

from alembic import op

revision = "0005_persistent_document_owner"
down_revision = "0004_content_hash_lookup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN owner_source_object_id TEXT")
    # Older documents did not record a distinct owner; retain their current
    # representative as the best available migration seed.
    op.execute("UPDATE documents SET owner_source_object_id = source_object_id")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN owner_source_object_id")
