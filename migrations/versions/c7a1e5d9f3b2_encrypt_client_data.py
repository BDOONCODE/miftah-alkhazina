"""encrypt client data at rest

Client names, IBANs, identifiers, transfer details, support messages and audit details become AES-GCM
ciphertext (app/crypto.py). IBANs that are looked up also get a blind index (iban_hash).
Needs DATA_ENCRYPTION_KEY in production.

Revision ID: c7a1e5d9f3b2
Revises: b4e81c2d7a90
Create Date: 2026-11-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app import crypto


# revision identifiers, used by Alembic.
revision: str = 'c7a1e5d9f3b2'
down_revision: Union[str, Sequence[str], None] = 'b4e81c2d7a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# table -> columns now stored as ciphertext
ENCRYPTED = {
    'entities': ['name', 'cr_number', 'vat_number', 'iban', 'contact_name', 'contact_phone', 'pool_virtual_iban'],
    'bank_accounts': ['name', 'iban'],
    'buckets': ['virtual_iban'],
    'policy_buckets': ['destination'],
    'transactions': ['note'],
    'payouts': ['beneficiary_name', 'beneficiary_iban'],
    'virtual_ibans': ['iban'],
    'held_deposits': ['sender_iban', 'sender_name'],
    'transfer_issues': ['destination'],
    'reversal_requests': ['reason', 'note'],
    'support_tickets': ['contact', 'subject', 'body', 'reply'],
    'audit_log': ['details'],
}
HASHED = ('bank_accounts', 'virtual_ibans')  # tables whose iban is looked up


def _transform(conn, fn, hash_ibans: bool) -> None:
    for table, columns in ENCRYPTED.items():
        rows = conn.execute(sa.text(f"SELECT id, {', '.join(columns)} FROM {table}")).mappings().all()
        for row in rows:
            values = {c: fn(row[c]) if isinstance(row[c], str) else row[c] for c in columns}
            if hash_ibans and table in HASHED:
                values['iban_hash'] = crypto.iban_index(crypto.decrypt(row['iban']))
            if values == {c: row[c] for c in columns}:
                continue
            assignments = ', '.join(f"{c} = :{c}" for c in values)
            conn.execute(sa.text(f"UPDATE {table} SET {assignments} WHERE id = :id"), {**values, 'id': row['id']})


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    postgres = conn.dialect.name == 'postgresql'

    for table in HASHED:
        op.add_column(table, sa.Column('iban_hash', sa.String(length=64), nullable=True))

    if postgres:
        # Ciphertext is longer than the old length limits; JSON details become an encrypted string
        for table, columns in ENCRYPTED.items():
            for column in columns:
                op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT USING {column}::text")
        # Uniqueness on ciphertext means nothing (a fresh nonce every time): it moves to the blind index
        op.execute("ALTER TABLE bank_accounts DROP CONSTRAINT IF EXISTS bank_accounts_entity_id_iban_key")
        op.execute("ALTER TABLE virtual_ibans DROP CONSTRAINT IF EXISTS uq_virtual_ibans_iban")
    op.drop_index('ix_bank_accounts_iban', table_name='bank_accounts')

    _transform(conn, crypto.encrypt, hash_ibans=True)

    op.create_index('ix_bank_accounts_iban_hash', 'bank_accounts', ['iban_hash'])
    op.create_index('uq_bank_accounts_entity_iban_hash', 'bank_accounts', ['entity_id', 'iban_hash'], unique=True)
    op.create_index('uq_virtual_ibans_iban_hash', 'virtual_ibans', ['iban_hash'], unique=True)


def downgrade() -> None:
    """Downgrade schema: decrypt everything back (the columns stay TEXT)."""
    conn = op.get_bind()
    op.drop_index('uq_virtual_ibans_iban_hash', table_name='virtual_ibans')
    op.drop_index('uq_bank_accounts_entity_iban_hash', table_name='bank_accounts')
    op.drop_index('ix_bank_accounts_iban_hash', table_name='bank_accounts')
    _transform(conn, crypto.decrypt, hash_ibans=False)
    op.create_index('ix_bank_accounts_iban', 'bank_accounts', ['iban'])
    for table in HASHED:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column('iban_hash')
