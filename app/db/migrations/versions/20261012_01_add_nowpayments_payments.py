"""Add isolated NOWPayments payment storage.

Revision ID: 20261012_01
Revises:
Create Date: 2026-10-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nowpayments_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(length=100), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=False),
        sa.Column("price_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("price_currency", sa.String(length=10), nullable=False),
        sa.Column("pay_currency", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=100), nullable=True),
        sa.Column("invoice_id", sa.String(length=100), nullable=True),
        sa.Column("invoice_url", sa.String(length=2048), nullable=True),
        sa.Column("pay_address", sa.String(length=255), nullable=True),
        sa.Column("pay_amount", sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column("payment_status", sa.String(length=30), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id", name="uq_nowpayments_payments_invoice_id"),
    )
    op.create_index(
        "ix_nowpayments_payments_user_id",
        "nowpayments_payments",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_nowpayments_payments_order_id",
        "nowpayments_payments",
        ["order_id"],
        unique=True,
    )
    op.create_index(
        "ix_nowpayments_payments_provider_payment_id",
        "nowpayments_payments",
        ["provider_payment_id"],
        unique=True,
    )
    op.create_index(
        "ix_nowpayments_payments_payment_status",
        "nowpayments_payments",
        ["payment_status"],
        unique=False,
    )
    op.create_index(
        "ix_nowpayments_payments_user_status",
        "nowpayments_payments",
        ["user_id", "payment_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_nowpayments_payments_user_status",
        table_name="nowpayments_payments",
    )
    op.drop_index(
        "ix_nowpayments_payments_payment_status",
        table_name="nowpayments_payments",
    )
    op.drop_index(
        "ix_nowpayments_payments_provider_payment_id",
        table_name="nowpayments_payments",
    )
    op.drop_index(
        "ix_nowpayments_payments_order_id",
        table_name="nowpayments_payments",
    )
    op.drop_index(
        "ix_nowpayments_payments_user_id",
        table_name="nowpayments_payments",
    )
    op.drop_table("nowpayments_payments")
