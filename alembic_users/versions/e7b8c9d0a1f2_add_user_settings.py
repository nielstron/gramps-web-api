"""Add private per-user settings.

Revision ID: e7b8c9d0a1f2
Revises: d4e9a1c7b3f2
Create Date: 2026-09-02 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "e7b8c9d0a1f2"
down_revision = "d4e9a1c7b3f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("settings", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("users", "settings")
