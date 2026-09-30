"""Add user.disabled_externally: a disable sauron learned about, not one it made

The renewal page's credential check records ``is_disabled = True`` when it
finds Jellyfin already has the account disabled. That is usually a failed-login
lockout, which a password reset is meant to lift — but ``is_disabled`` alone
reads like a disable sauron made on purpose (expiry, the admin's button), which
the reset must leave alone. This column tells the two apart.

Additive, NOT NULL with a server default of false: every existing row reads
"not learned", which is what they all are. ``recreate="never"`` on purpose —
SQLite can ADD COLUMN in place, and a batch rebuild of ``user`` would DROP it,
which with foreign keys on fires every ON DELETE CASCADE in its children (see
20260928_no_id_reuse).

Revision ID: 20260930_disabled_ext
Revises: 20260929_bound_email
Create Date: 2026-09-30

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260930_disabled_ext"
down_revision = "20260929_bound_email"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    return "disabled_externally" in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("user")
    }


def upgrade():
    if _has_column():
        return
    with op.batch_alter_table("user", schema=None, recreate="never") as batch_op:
        batch_op.add_column(
            sa.Column(
                "disabled_externally",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade():
    if not _has_column():
        return
    with op.batch_alter_table("user", schema=None, recreate="never") as batch_op:
        batch_op.drop_column("disabled_externally")
