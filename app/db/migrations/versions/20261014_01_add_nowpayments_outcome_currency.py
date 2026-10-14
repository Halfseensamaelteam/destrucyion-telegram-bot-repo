"""Persist selected NOWPayments payout currency.

Revision ID: 20261014_01
Revises: 20261012_01
Create Date: 2026-10-14
"""
from alembic import op
import sqlalchemy as sa

revision = "20261014_01"
down_revision = "20261012_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "nowpayments_payments",
        sa.Column(
            "outcome_currency",
            sa.String(length=32),
            nullable=False,
            server_default="usdcbase",
        ),
    )


def downgrade() -> None:
    op.drop_column("nowpayments_payments", "outcome_currency")
