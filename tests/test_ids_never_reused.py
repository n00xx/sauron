"""Invitation and user ids must never be handed out twice.

Without AUTOINCREMENT, SQLite gives a new row ``max(rowid) + 1``: delete the
newest invitation and the next one gets its id again. Measured in production on
2026-09-28 — invitation 35 was deleted, then a new purchase got 35 again. The
storefront (neexy) keeps these ids on its orders and later acts on them: a
refund disables every user of ``invitation.id`` and deletes it, a renewal
refund rewrites ``user.id``'s expiry. With reuse, refunding an OLD order hits a
DIFFERENT, paying customer.

The migration rebuilds both tables with AUTOINCREMENT. Rebuilding a PARENT
table is where data dies: ``DROP TABLE`` with foreign keys enabled runs an
implicit ``DELETE`` that fires every ``ON DELETE CASCADE`` / ``SET NULL`` in
the child tables — and ``PRAGMA foreign_keys = OFF`` is silently ignored
inside a transaction. So these tests seed children on both sides and assert
that not one row changes.
"""

import os
import sqlite3
import tempfile

import pytest
from flask_migrate import upgrade

from app import create_app
from app.config import BaseConfig

PREVIOUS_HEAD = "20260914_moonbase_notice"


class _Config(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = None


@pytest.fixture
def db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        yield path
    finally:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)


@pytest.fixture
def migration_app(db_path):
    config = _Config()
    config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
    return create_app(config)  # type: ignore[arg-type]


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_FILLER = {
    "INTEGER": 0,
    "BOOLEAN": 0,
    "FLOAT": 0.0,
    "DATETIME": "2026-01-01 00:00:00",
    "DATE": "2026-01-01",
    "JSON": "{}",
}


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> int:
    """Insert a row, filling every NOT NULL column without a default."""
    row = dict(values)
    for _cid, name, ctype, notnull, default, pk in conn.execute(
        f'PRAGMA table_info("{table}")'
    ):
        if name in row or pk or not notnull or default is not None:
            continue
        base = (ctype or "").split("(")[0].upper()
        row[name] = _FILLER.get(base, f"{table}-{name}")
    cols = ", ".join(f'"{c}"' for c in row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(
        f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', list(row.values())
    )
    return cur.lastrowid


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def _seed(conn: sqlite3.Connection) -> dict:
    server = _insert(conn, "media_server", {"name": "jf", "server_type": "jellyfin"})
    library = _insert(
        conn, "library", {"external_id": "lib1", "name": "Movies", "server_id": server}
    )
    users = [
        _insert(conn, "user", {"username": f"user{i}", "token": f"t{i}", "code": f"C{i}"})
        for i in range(1, 4)
    ]
    invites = [
        _insert(conn, "invitation", {"code": f"CODE{i}", "used_by_id": users[i - 1]})
        for i in range(1, 4)
    ]
    for inv, user in zip(invites, users, strict=True):
        conn.execute(
            "INSERT INTO invitation_user (invite_id, user_id, used_at, server_id) "
            "VALUES (?, ?, '2026-01-01 00:00:00', ?)",
            (inv, user, server),
        )
        conn.execute(
            "INSERT INTO invitation_server (invite_id, server_id, used) VALUES (?, ?, 0)",
            (inv, server),
        )
        conn.execute(
            "INSERT INTO invite_library (invite_id, library_id) VALUES (?, ?)",
            (inv, library),
        )
    return {"users": users, "invites": invites}


# Every table that holds a foreign key to invitation or user — the rows a
# cascading DROP would destroy or null out.
CHILD_TABLES = ("invitation_user", "invitation_server", "invite_library")


def test_migration_keeps_every_row_and_stops_id_reuse(migration_app, db_path):
    with migration_app.app_context():
        upgrade(revision=PREVIOUS_HEAD)

    conn = _connect(db_path)
    seeded = _seed(conn)
    # Reproduce the production history: the newest invitation and user were
    # deleted, so without the fix their ids come back.
    deleted_invite = seeded["invites"][-1]
    deleted_user = seeded["users"][-1]
    conn.execute("DELETE FROM invitation WHERE id = ?", (deleted_invite,))
    conn.execute('DELETE FROM "user" WHERE id = ?', (deleted_user,))
    before = {t: _count(conn, t) for t in ("invitation", "user", *CHILD_TABLES)}
    used_by_before = conn.execute(
        "SELECT id, used_by_id FROM invitation ORDER BY id"
    ).fetchall()
    conn.close()

    with migration_app.app_context():
        upgrade()

    conn = _connect(db_path)
    after = {t: _count(conn, t) for t in before}
    assert after == before, "a table lost rows during the rebuild"
    assert (
        conn.execute("SELECT id, used_by_id FROM invitation ORDER BY id").fetchall()
        == used_by_before
    ), "ON DELETE SET NULL fired on invitation.used_by_id"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    for table in ("invitation", "user"):
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        assert "AUTOINCREMENT" in ddl.upper(), f"{table} was not rebuilt"

    new_invite = _insert(conn, "invitation", {"code": "NEWCODE"})
    new_user = _insert(conn, "user", {"username": "fresh", "token": "tf", "code": "CF"})
    assert new_invite > deleted_invite, "invitation id handed out twice"
    assert new_user > deleted_user, "user id handed out twice"

    # And from now on, deleting the newest row no longer frees its id.
    conn.execute("DELETE FROM invitation WHERE id = ?", (new_invite,))
    again = _insert(conn, "invitation", {"code": "AGAIN"})
    assert again > new_invite
    conn.close()


def test_indexes_survive_the_rebuild(migration_app, db_path):
    with migration_app.app_context():
        upgrade(revision=PREVIOUS_HEAD)
    conn = _connect(db_path)

    def named_indexes():
        return sorted(
            conn.execute(
                "SELECT tbl_name, name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name IN ('invitation', 'user') "
                "AND sql IS NOT NULL"
            ).fetchall()
        )

    before = named_indexes()
    conn.close()
    with migration_app.app_context():
        upgrade()
    conn = _connect(db_path)
    assert named_indexes() == before
    conn.close()


def test_fresh_install_starts_ids_at_one(migration_app, db_path):
    """The safety margin only applies to tables that already have history."""
    with migration_app.app_context():
        upgrade()
    conn = _connect(db_path)
    assert _insert(conn, "invitation", {"code": "FIRST"}) == 1
    conn.close()
