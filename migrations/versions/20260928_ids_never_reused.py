"""Never hand out an invitation or user id twice (SQLite AUTOINCREMENT)

Revision ID: 20260928_no_id_reuse
Revises: 20260914_moonbase_notice
Create Date: 2026-09-28

Without AUTOINCREMENT, SQLite assigns ``max(rowid) + 1``: delete the newest
invitation and the next one gets its id again (measured in production on
2026-09-28: invitation 35 was issued twice). The storefront keeps these ids on
its orders and acts on them much later — a refund disables every user of an
invitation id and deletes it, a renewal refund rewrites a user id's expiry — so
a reused id turns one customer's refund into another customer's lockout.

AUTOINCREMENT can only be declared at CREATE TABLE time, so both tables are
rebuilt with SQLite's own "generalized ALTER TABLE" procedure
(sqlite.org/lang_altertable.html#otheralter). The load-bearing part is step 1:
these are PARENT tables, and ``DROP TABLE`` with foreign keys on runs an
implicit DELETE that fires every ON DELETE CASCADE / SET NULL in the children
(invitation_user, invitation_server, invite_library, invitation.used_by_id,
stripe_event, …). ``PRAGMA foreign_keys = OFF`` is a silent no-op inside a
transaction, so it is issued on the raw connection in autocommit mode and then
READ BACK — if it did not take, nothing is touched.

Guard rails, all inside one transaction that rolls back on any failure:
  * every table's row count is identical before and after;
  * ``PRAGMA foreign_key_check`` finds nothing;
  * the rebuilt DDL is the live DDL with only the primary key changed — no
    columns, defaults or constraints are re-typed from the models.

The id sequence is seeded ``ID_MARGIN`` above the current maximum, because the
rows deleted from the top before this migration (35 in production) are gone
and nothing else records how high the ids once went.
"""

import re

from alembic import op

revision = "20260928_no_id_reuse"
down_revision = "20260914_moonbase_notice"
branch_labels = None
depends_on = None

TABLES = ("invitation", "user")

# Head-room above the current max id. Only has to exceed the number of rows
# ever deleted from the top of each table — dozens in this deployment.
ID_MARGIN = 1000

_PK_COLUMN = re.compile(r"(\n\s*\"?id\"?\s+INTEGER\s+NOT\s+NULL)\s*,", re.IGNORECASE)
_PK_CONSTRAINT = re.compile(r",\s*PRIMARY\s+KEY\s*\(\s*\"?id\"?\s*\)", re.IGNORECASE)


def _autoincrement_ddl(ddl: str, table: str, new_name: str) -> str:
    """The live CREATE TABLE, renamed, with ``id`` as INTEGER PRIMARY KEY AUTOINCREMENT."""
    head = re.compile(rf'^CREATE TABLE\s+"?{re.escape(table)}"?\s*\(', re.IGNORECASE)
    if not head.search(ddl):
        raise RuntimeError(f"unexpected DDL header for {table}: {ddl[:80]!r}")
    if len(_PK_COLUMN.findall(ddl)) != 1 or len(_PK_CONSTRAINT.findall(ddl)) != 1:
        # Refuse to guess: a different shape means a schema this migration was
        # not written against.
        raise RuntimeError(f"unexpected primary key shape for {table}")
    out = head.sub(f'CREATE TABLE "{new_name}" (', ddl, count=1)
    out = _PK_COLUMN.sub(r"\1 PRIMARY KEY AUTOINCREMENT,", out, count=1)
    return _PK_CONSTRAINT.sub("", out, count=1)


def _row_counts(cur) -> dict[str, int]:
    names = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    return {n: cur.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _rebuild(cur, table: str) -> None:
    ddl = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    if "AUTOINCREMENT" in ddl.upper():
        return  # already rebuilt (re-run, or created by a newer schema)

    new_name = f"{table}__autoinc"
    extras = cur.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    columns = ", ".join(
        f'"{r[1]}"' for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()
    )
    max_id = cur.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"').fetchone()[0]

    cur.execute(_autoincrement_ddl(ddl, table, new_name))
    cur.execute(
        f'INSERT INTO "{new_name}" ({columns}) SELECT {columns} FROM "{table}"'
    )
    cur.execute(f'DROP TABLE "{table}"')
    cur.execute(f'ALTER TABLE "{new_name}" RENAME TO "{table}"')
    for (sql,) in extras:
        cur.execute(sql)

    # A table with no history keeps ids from 1; one with history starts well
    # clear of anything that may have been deleted from its top.
    cur.execute("DELETE FROM sqlite_sequence WHERE name IN (?, ?)", (table, new_name))
    if max_id > 0:
        cur.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            (table, max_id + ID_MARGIN),
        )


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return  # other engines never reuse sequence values

    with op.get_context().autocommit_block():
        raw = bind.connection.driver_connection
        previous_isolation = raw.isolation_level
        raw.isolation_level = None  # we issue BEGIN/COMMIT ourselves
        cur = raw.cursor()
        try:
            cur.execute("PRAGMA foreign_keys = OFF")
            if cur.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise RuntimeError(
                    "could not disable foreign keys (open transaction?) — "
                    "refusing to rebuild parent tables"
                )
            cur.execute("BEGIN IMMEDIATE")
            try:
                before = _row_counts(cur)
                for table in TABLES:
                    _rebuild(cur, table)
                after = _row_counts(cur)
                changed = {
                    t: (before.get(t), after.get(t))
                    for t in set(before) | set(after)
                    if not t.endswith("__autoinc") and before.get(t) != after.get(t)
                }
                if changed:
                    raise RuntimeError(f"row counts changed during rebuild: {changed}")
                violations = cur.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(f"foreign key violations: {violations[:5]}")
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        finally:
            cur.execute("PRAGMA foreign_keys = ON")
            cur.close()
            raw.isolation_level = previous_isolation


def downgrade():
    # AUTOINCREMENT only stops id reuse; nothing older depends on reuse, so
    # there is nothing to undo (and a rebuild back would be pure risk).
    pass
