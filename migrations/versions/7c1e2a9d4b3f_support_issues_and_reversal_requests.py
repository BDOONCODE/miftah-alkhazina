"""support: transfer issues and reversal requests

Revision ID: 7c1e2a9d4b3f
Revises: 2558d75d19ce
Create Date: 2026-10-12 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models


# revision identifiers, used by Alembic.
revision: str = '7c1e2a9d4b3f'
down_revision: Union[str, Sequence[str], None] = '2558d75d19ce'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'transfer_issues',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('leg', sa.String(length=16), nullable=False),
        sa.Column('destination', sa.String(length=128), nullable=False),
        sa.Column('reference', sa.String(length=64), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('resolved', sa.Boolean(), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('resolved_by', sa.Integer(), nullable=True),
        sa.Column('created_at', app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], name='fk_transfer_issues_entity_id_entities'),
        sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], name='fk_transfer_issues_resolved_by_users'),
        sa.PrimaryKeyConstraint('id', name='pk_transfer_issues'),
    )
    op.create_index('ix_transfer_issues_entity_id', 'transfer_issues', ['entity_id'])
    op.create_index('ix_transfer_issues_created_at', 'transfer_issues', ['created_at'])
    op.create_table(
        'reversal_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('requested_by', sa.Integer(), nullable=False),
        sa.Column('handled_by', sa.Integer(), nullable=True),
        sa.Column('created_at', app.models.UTCDateTime(), nullable=False),
        sa.Column('updated_at', app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], name='fk_reversal_requests_entity_id_entities'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], name='fk_reversal_requests_transaction_id_transactions'),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name='fk_reversal_requests_requested_by_users'),
        sa.ForeignKeyConstraint(['handled_by'], ['users.id'], name='fk_reversal_requests_handled_by_users'),
        sa.PrimaryKeyConstraint('id', name='pk_reversal_requests'),
    )
    op.create_index('ix_reversal_requests_entity_id', 'reversal_requests', ['entity_id'])
    op.create_index('ix_reversal_requests_transaction_id', 'reversal_requests', ['transaction_id'])
    op.create_index('ix_reversal_requests_created_at', 'reversal_requests', ['created_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('reversal_requests')
    op.drop_table('transfer_issues')
