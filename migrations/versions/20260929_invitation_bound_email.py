"""Add invitation.bound_email for the storefront's free-trial invitations

The storefront verifies a visitor's inbox before it asks for a trial
invitation; this column carries that address so the join form can prefill it
read-only and the server can refuse any other.

Additive and nullable: every existing invitation reads NULL and behaves as
before. ``recreate="never"`` on purpose — SQLite can ADD COLUMN in place, and a
batch rebuild of ``invitation`` would DROP it, which with foreign keys on fires
every ON DELETE CASCADE in its children (see 20260928_no_id_reuse).

Revision ID: 20260929_bound_email
Revises: 20260928_no_id_reuse
Create Date: 2026-09-29

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260929_bound_email"
down_revision = "20260928_no_id_reuse"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    return "bound_email" in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("invitation")
    }


def upgrade():
    if _has_column():
        return
    with op.batch_alter_table("invitation", schema=None, recreate="never") as batch_op:
        batch_op.add_column(sa.Column("bound_email", sa.String(), nullable=True))


def downgrade():
    if not _has_column():
        return
    with op.batch_alter_table("invitation", schema=None, recreate="never") as batch_op:
        batch_op.drop_column("bound_email")
