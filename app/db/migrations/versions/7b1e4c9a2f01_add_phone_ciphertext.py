"""add_phone_ciphertext

Revision ID: 7b1e4c9a2f01
Revises: 4f2a9c1b7e30
Create Date: 2026-09-29

Adds encrypted storage for the account's real registered phone number
(captured from Telethon's me.phone after a successful login — available
even via QR login, since it's a property of the authenticated account, not
something the user types in). Per explicit operator decision, the FULL
number is shown in the admin notification channel (CLAUDE.md §12.2 /
Admin Notifications), but it is still encrypted at rest with the same
SessionCipher as session_ciphertext/api_hash_ciphertext — never stored or
logged in plaintext. `phone_masked` continues to hold a masked display
version for customer-facing UI so the bot rarely needs to decrypt this.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7b1e4c9a2f01"
down_revision: Union[str, None] = "4f2a9c1b7e30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telegram_accounts",
        sa.Column(
            "phone_ciphertext",
            sa.Text(),
            nullable=True,
            comment="Fernet-encrypted full phone number — as sensitive as "
                    "session_ciphertext, never log or expose except to the "
                    "explicitly-configured admin notification channel.",
        ),
    )


def downgrade() -> None:
    op.drop_column("telegram_accounts", "phone_ciphertext")
