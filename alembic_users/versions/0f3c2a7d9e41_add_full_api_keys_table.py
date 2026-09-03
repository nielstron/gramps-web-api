"""add full API keys table

Revision ID: 0f3c2a7d9e41
Revises: e7b8c9d0a1f2
Create Date: 2026-09-03

"""

import sqlalchemy as sa
from alembic import op

from gramps_webapi.auth.sql_guid import GUID

revision = "0f3c2a7d9e41"
down_revision = "e7b8c9d0a1f2"
branch_labels = None
depends_on = None


def upgrade():
    """Create the API key table."""
    inspector = sa.inspect(op.get_bind())
    if "api_keys" in inspector.get_table_names():
        return
    op.create_table(
        "api_keys",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("user_id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("jti", sa.String(length=36), nullable=False),
        sa.Column("fingerprint", sa.String(length=12), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"], unique=False)
    op.create_index("ix_api_keys_jti", "api_keys", ["jti"], unique=True)
    op.create_index("ix_api_keys_expires_at", "api_keys", ["expires_at"], unique=False)


def downgrade():
    """Drop the API key table."""
    op.drop_index("ix_api_keys_expires_at", table_name="api_keys")
    op.drop_index("ix_api_keys_jti", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")
