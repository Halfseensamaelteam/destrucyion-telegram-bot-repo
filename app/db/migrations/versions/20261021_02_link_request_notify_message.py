"""Add notify message ids to link_requests (single edited status message).

Revision ID: 20261021_02
Revises: 20261021_01
Create Date: 2026-10-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261021_02"
down_revision: Union[str, None] = "20261021_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "link_requests", sa.Column("notify_chat_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "link_requests", sa.Column("notify_message_id", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("link_requests", "notify_message_id")
    op.drop_column("link_requests", "notify_chat_id")
