"""fix_api_hash_encryption_and_remove_phone_flow

Revision ID: 4f2a9c1b7e30
Revises: ecb24f5720c3
Create Date: 2026-09-17

SECURITY FIX: the `api_hash` column added in ecb24f5720c3 stored this value
in PLAINTEXT. api_hash is as sensitive as a Telegram session string
(CLAUDE.md §12.2/§13) and must be encrypted at rest with the same
SessionCipher used for session_ciphertext.

This migration renames the column to make the encryption requirement
explicit in the schema itself (api_hash_ciphertext), matching the existing
session_ciphertext convention. Any api_hash value already stored in the old
plaintext column is NOT migrated forward automatically — plaintext secrets
that were already written to the database should be treated as compromised;
affected users must reconnect via /connect to re-supply their api_hash so it
gets encrypted going forward.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4f2a9c1b7e30"
down_revision: Union[str, None] = "ecb24f5720c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the plaintext column entirely rather than renaming-in-place —
    # any value in it was written unencrypted and must not be reused as if
    # it were ciphertext.
    with op.batch_alter_table("telegram_accounts", schema=None) as batch_op:
        batch_op.drop_column("api_hash")
        batch_op.add_column(
            sa.Column(
                "api_hash_ciphertext",
                sa.Text(),
                nullable=True,
                comment="Fernet-encrypted api_hash for THIS account — as "
                        "sensitive as session_ciphertext, never log or expose",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("telegram_accounts", schema=None) as batch_op:
        batch_op.drop_column("api_hash_ciphertext")
        batch_op.add_column(
            sa.Column("api_hash", sa.String(length=255), nullable=True)
        )
