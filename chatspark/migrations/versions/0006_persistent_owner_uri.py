"""retain owner URL when its source object is absent from a fresh candidate

Revision ID: 0006_persistent_owner_uri
Revises: 0005_persistent_document_owner
"""

from alembic import op

revision = "0006_persistent_owner_uri"
down_revision = "0005_persistent_document_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN owner_canonical_uri TEXT")
    op.execute(
        """UPDATE documents SET owner_canonical_uri = COALESCE(
               (SELECT canonical_uri FROM source_objects
                WHERE source_objects.source_object_id = documents.owner_source_object_id),
               canonical_uri)"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN owner_canonical_uri")
