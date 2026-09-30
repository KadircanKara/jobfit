"""work authorization and visa sponsorship facts per job

The fit gate reads each posting for where it requires the applicant to already
be authorized to work, and whether it offers visa sponsorship. Stored on the
job so stage 1 can apply the user's authorization settings without asking the
model again.

Revision ID: c4a9e1d2f7b3
Revises: b7e2d41a9c30
Create Date: 2026-09-30 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4a9e1d2f7b3'
down_revision: str | Sequence[str] | None = 'b7e2d41a9c30'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('work_auth_required', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('visa_sponsorship', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column('auth_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('auth_checked_at')
        batch_op.drop_column('visa_sponsorship')
        batch_op.drop_column('work_auth_required')
