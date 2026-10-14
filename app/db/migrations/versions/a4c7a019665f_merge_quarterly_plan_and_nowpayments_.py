"""Merge quarterly plan and NOWPayments payout currency migrations

Revision ID: a4c7a019665f
Revises: 20261013_01, 20261014_01
Create Date: 2026-10-14 08:09:31.921240+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = 'a4c7a019665f'
down_revision: Union[str, None] = ('20261013_01', '20261014_01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
