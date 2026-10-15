"""deposit ibans and held deposits

Revision ID: b4e81c2d7a90
Revises: 9d2b7e4f1a6c
Create Date: 2026-10-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models


# revision identifiers, used by Alembic.
revision: str = 'b4e81c2d7a90'
down_revision: Union[str, Sequence[str], None] = '9d2b7e4f1a6c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'virtual_ibans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('iban', sa.String(length=34), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('last_entity_id', sa.Integer(), nullable=True),
        sa.Column('assigned_at', app.models.UTCDateTime(), nullable=True),
        sa.Column('released_at', app.models.UTCDateTime(), nullable=True),
        sa.Column('created_at', app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], name='fk_virtual_ibans_entity_id_entities'),
        sa.ForeignKeyConstraint(['last_entity_id'], ['entities.id'], name='fk_virtual_ibans_last_entity_id_entities'),
        sa.PrimaryKeyConstraint('id', name='pk_virtual_ibans'),
        sa.UniqueConstraint('iban', name='uq_virtual_ibans_iban'),
    )
    op.create_index('ix_virtual_ibans_status', 'virtual_ibans', ['status'])
    op.create_index('ix_virtual_ibans_entity_id', 'virtual_ibans', ['entity_id'])
    op.create_table(
        'held_deposits',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('sender_iban', sa.String(length=34), nullable=False),
        sa.Column('sender_name', sa.String(length=128), nullable=False),
        sa.Column('amount', sa.BigInteger(), nullable=False),
        sa.Column('occurred_at', app.models.UTCDateTime(), nullable=False),
        sa.Column('reference', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('decided_by', sa.Integer(), nullable=True),
        sa.Column('decided_at', app.models.UTCDateTime(), nullable=True),
        sa.Column('created_at', app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], name='fk_held_deposits_entity_id_entities'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], name='fk_held_deposits_transaction_id_transactions'),
        sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name='fk_held_deposits_decided_by_users'),
        sa.PrimaryKeyConstraint('id', name='pk_held_deposits'),
        sa.UniqueConstraint('reference', name='uq_held_deposits_reference'),
    )
    op.create_index('ix_held_deposits_entity_id', 'held_deposits', ['entity_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('held_deposits')
    op.drop_table('virtual_ibans')
