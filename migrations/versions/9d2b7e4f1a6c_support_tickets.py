"""support tickets

Revision ID: 9d2b7e4f1a6c
Revises: 7c1e2a9d4b3f
Create Date: 2026-10-12 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models


# revision identifiers, used by Alembic.
revision: str = '9d2b7e4f1a6c'
down_revision: Union[str, Sequence[str], None] = '7c1e2a9d4b3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'support_tickets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('contact', sa.String(length=254), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reply', sa.Text(), nullable=False),
        sa.Column('handled_by', sa.Integer(), nullable=True),
        sa.Column('created_at', app.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_support_tickets_user_id_users'),
        sa.ForeignKeyConstraint(['handled_by'], ['users.id'], name='fk_support_tickets_handled_by_users'),
        sa.PrimaryKeyConstraint('id', name='pk_support_tickets'),
    )
    op.create_index('ix_support_tickets_user_id', 'support_tickets', ['user_id'])
    op.create_index('ix_support_tickets_created_at', 'support_tickets', ['created_at'])
    op.create_table(
        'support_ticket_images',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('mime', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['ticket_id'], ['support_tickets.id'], name='fk_support_ticket_images_ticket_id_support_tickets'),
        sa.PrimaryKeyConstraint('id', name='pk_support_ticket_images'),
    )
    op.create_index('ix_support_ticket_images_ticket_id', 'support_ticket_images', ['ticket_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('support_ticket_images')
    op.drop_table('support_tickets')
