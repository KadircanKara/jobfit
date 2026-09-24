"""client payment verification and lifetime spend

Two facts about who is paying, needed because a rule now reads them: an
Upwork run surfaced clients with $0.00 lifetime spend and no way to filter
them out. Null everywhere else, and null means unknown rather than zero -
the house rule that unknown is never a rejection depends on being able to
tell the two apart.

Revision ID: a3f1c07d2b58
Revises: 90c0b9caf29f
Create Date: 2026-09-02 21:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f1c07d2b58'
down_revision: str | Sequence[str] | None = '90c0b9caf29f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_verified', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('client_total_spent', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('client_total_spent')
        batch_op.drop_column('client_verified')
