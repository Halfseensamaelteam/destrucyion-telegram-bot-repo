"""Add the quarterly subscription plan to the PostgreSQL enum.

Revision ID: 20261013_01
Revises: 20261012_01
Create Date: 2026-10-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20261013_01"
down_revision: Union[str, None] = "20261012_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE subscriptionplan ADD VALUE IF NOT EXISTS 'quarterly'")


def downgrade() -> None:
    # PostgreSQL cannot safely remove an enum value in-place.
    pass
