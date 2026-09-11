"""Add pending user invitations.

Revision ID: 41bc78f029da
Revises: 0f3c2a7d9e41
"""

import sqlalchemy as sa
from alembic import op

revision = "41bc78f029da"
down_revision = "0f3c2a7d9e41"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", sa.Integer(), nullable=False),
        sa.Column("tree", sa.String(), nullable=True),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("email", "tree", name="uq_user_invitations_email_tree"),
    )
    op.create_index("ix_user_invitations_tree", "user_invitations", ["tree"])


def downgrade():
    op.drop_table("user_invitations")
