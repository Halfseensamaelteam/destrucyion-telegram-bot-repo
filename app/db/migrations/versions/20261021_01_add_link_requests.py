"""Add the link_requests table (bot-submitted post/invite links).

Revision ID: 20261021_01
Revises: a4c7a019665f
Create Date: 2026-10-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261021_01"
down_revision: Union[str, None] = "a4c7a019665f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type idempotently: a previous failed attempt at this
    # migration may have left the type behind (CREATE TYPE inside the
    # migration transaction does not always roll back cleanly on every
    # driver), and SQLAlchemy's create_table would otherwise try to create
    # it a second time and fail with DuplicateObjectError. The column uses
    # create_type=False so create_table never re-creates it.
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE linkrequeststatus AS ENUM ('pending', 'processing', 'done', 'failed');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
        """
    )
    link_request_status = postgresql.ENUM(
        "pending",
        "processing",
        "done",
        "failed",
        name="linkrequeststatus",
        create_type=False,
    )
    op.create_table(
        "link_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "telegram_account_id",
            sa.Integer(),
            sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("raw_link", sa.Text(), nullable=False),
        sa.Column(
            "status",
            link_request_status,
            nullable=False,
            server_default="pending",
        ),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_link_requests_user_id", "link_requests", ["user_id"])
    op.create_index(
        "ix_link_requests_telegram_account_id", "link_requests", ["telegram_account_id"]
    )
    op.create_index("ix_link_requests_status", "link_requests", ["status"])
    op.create_index("ix_link_requests_created_at", "link_requests", ["created_at"])


def downgrade() -> None:
    op.drop_table("link_requests")
    op.execute("DROP TYPE IF EXISTS linkrequeststatus")
