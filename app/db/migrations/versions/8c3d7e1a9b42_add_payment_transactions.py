"""add payment transactions

Revision ID: 8c3d7e1a9b42
Revises: 7b1e4c9a2f01
Create Date: 2026-10-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8c3d7e1a9b42"
down_revision: Union[str, Sequence[str], None] = "7b1e4c9a2f01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE payment_status AS ENUM (
            'PENDING',
            'SETTLEMENT',
            'EXPIRE',
            'CANCEL',
            'DENY'
        )
        """
    )

    op.create_table(
        "payments",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "transaction_id",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "plan",
            sa.String(length=20),
            nullable=False,
        ),
        sa.Column(
            "amount",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "qr_string",
            sa.String(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        """
        ALTER TABLE payments
        ALTER COLUMN status TYPE payment_status
        USING status::payment_status
        """
    )

    op.create_index(
        "ix_payments_user_id",
        "payments",
        ["user_id"],
        unique=False,
    )

    op.create_index(
        "ix_payments_order_id",
        "payments",
        ["order_id"],
        unique=True,
    )

    op.create_index(
        "ix_payments_transaction_id",
        "payments",
        ["transaction_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payments_transaction_id",
        table_name="payments",
    )

    op.drop_index(
        "ix_payments_order_id",
        table_name="payments",
    )

    op.drop_index(
        "ix_payments_user_id",
        table_name="payments",
    )

    op.drop_table("payments")

    op.execute("DROP TYPE payment_status")