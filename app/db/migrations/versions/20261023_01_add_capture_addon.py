"""Add the Auto-Capture add-on tables (capture_subscriptions, capture_channels).

Revision ID: 20261023_01
Revises: 20261021_02
Create Date: 2026-10-23

capture_subscriptions mirrors subscriptions (same plan/status enums) but
is a separate product: the paid /capture add-on entitlement.
capture_channels holds the ONE registered channel/group per Telegram
account (UNIQUE on telegram_account_id enforces the 1-channel rule).

The subscriptionplan / subscriptionstatus enum types already exist in
PostgreSQL (created by the subscriptions migration), so both columns
reference them with create_type=False — no new types are created and
the downgrade must NOT drop them.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261023_01"
down_revision: Union[str, None] = "20261021_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    subscription_plan = postgresql.ENUM(
        "weekly",
        "monthly",
        "quarterly",
        "lifetime",
        name="subscriptionplan",
        create_type=False,
    )
    subscription_status = postgresql.ENUM(
        "active",
        "expired",
        "cancelled",
        name="subscriptionstatus",
        create_type=False,
    )

    op.create_table(
        "capture_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("plan", subscription_plan, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            subscription_status,
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "last_order_id",
            sa.String(length=100),
            nullable=True,
            comment="Most recent CAP-/NPC- order id that granted/extended this add-on",
        ),
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
    )
    op.create_index(
        "ix_capture_subscriptions_user_id", "capture_subscriptions", ["user_id"]
    )

    op.create_table(
        "capture_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "telegram_account_id",
            sa.Integer(),
            sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_capture_channels_telegram_account_id",
        "capture_channels",
        ["telegram_account_id"],
    )


def downgrade() -> None:
    op.drop_table("capture_channels")
    op.drop_table("capture_subscriptions")
    # NOTE: the subscriptionplan / subscriptionstatus types belong to the
    # subscriptions table and must NOT be dropped here.
