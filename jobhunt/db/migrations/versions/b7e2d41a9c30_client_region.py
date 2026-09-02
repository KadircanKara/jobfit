"""the client's state or region

Upwork reports the client's country and a state code. The country alone is
too coarse to disambiguate a company name on LinkedIn - searching "Nexora"
across "United States" misses the California company that a state-level
search finds first - so the region is worth storing rather than discarding.

Revision ID: b7e2d41a9c30
Revises: a3f1c07d2b58
Create Date: 2026-09-03 00:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e2d41a9c30'
down_revision: str | Sequence[str] | None = 'a3f1c07d2b58'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('client_region', sa.String(length=100), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('client_region')
