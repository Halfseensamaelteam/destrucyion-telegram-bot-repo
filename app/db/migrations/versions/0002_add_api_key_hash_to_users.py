"""Add api_key_hash to users table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "api_key_hash",
            sa.String(64),
            nullable=True,
            comment="SHA-256 hex digest of the user's API key. Raw key is never stored.",
        ),
    )
    op.create_index("ix_users_api_key_hash", "users", ["api_key_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_api_key_hash", table_name="users")
    op.drop_column("users", "api_key_hash")
