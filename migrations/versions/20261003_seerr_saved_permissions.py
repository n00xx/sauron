"""Add user.seerr_saved_permissions: Seerr permissions held while on the renewal screen

A member on the renewal screen can still sign in to Seerr, so sauron zeroes
their Seerr permissions and keeps the originals here until they renew.

Additive and nullable. ``recreate="never"`` on purpose — SQLite can ADD COLUMN
in place, and a batch rebuild of ``user`` would DROP it, which with foreign keys
on fires every ON DELETE CASCADE in its children (see 20260928_no_id_reuse).

Revision ID: 20261003_seerr_perms
Revises: 20261002_renewal_screen
Create Date: 2026-10-03

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20261003_seerr_perms"
down_revision = "20261002_renewal_screen"
branch_labels = None
depends_on = None

COLUMN = "seerr_saved_permissions"


def _has_column() -> bool:
    return COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("user")}


def upgrade():
    if _has_column():
        return
    with op.batch_alter_table("user", schema=None, recreate="never") as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))


def downgrade():
    if not _has_column():
        return
    with op.batch_alter_table("user", schema=None, recreate="never") as batch_op:
        batch_op.drop_column(COLUMN)
