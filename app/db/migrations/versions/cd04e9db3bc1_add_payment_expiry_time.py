"""add payment expiry time

Revision ID: cd04e9db3bc1
Revises: 8c3d7e1a9b42
Create Date: 2026-10-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers
revision: str = "cd04e9db3bc1"
down_revision: Union[str, None] = "8c3d7e1a9b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column(
            "expiry_time",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("payments", "expiry_time")
