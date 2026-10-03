"""Add user.restricted_policy and user.moonbase_notice_lapsed for the renewal screen

`restricted_policy` holds, while an account is on the renewal screen, the policy
fields the restriction overrode — what renewal puts back. `moonbase_notice_lapsed`
marks the member's Moonbase notice as the "membership ended" one.

Additive: a nullable text column, and a NOT NULL boolean with a server default
of false, so every existing row reads "not restricted, no lapsed notice", which
is what they all are. ``recreate="never"`` on purpose — SQLite can ADD COLUMN in
place, and a batch rebuild of ``user`` would DROP it, which with foreign keys on
fires every ON DELETE CASCADE in its children (see 20260928_no_id_reuse).

Revision ID: 20261002_renewal_screen
Revises: 20260930_disabled_ext
Create Date: 2026-10-02

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20261002_renewal_screen"
down_revision = "20260930_disabled_ext"
branch_labels = None
depends_on = None

COLUMNS = ("restricted_policy", "moonbase_notice_lapsed")


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("user")}


def upgrade():
    existing = _existing()
    with op.batch_alter_table("user", schema=None, recreate="never") as batch_op:
        if "restricted_policy" not in existing:
            batch_op.add_column(
                sa.Column("restricted_policy", sa.Text(), nullable=True)
            )
        if "moonbase_notice_lapsed" not in existing:
            batch_op.add_column(
                sa.Column(
                    "moonbase_notice_lapsed",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )


def downgrade():
    existing = _existing()
    with op.batch_alter_table("user", schema=None, recreate="never") as batch_op:
        for column in COLUMNS:
            if column in existing:
                batch_op.drop_column(column)
