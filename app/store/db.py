"""SQLite access. One connection per thread (threading.local), WAL mode,
and a process-wide write lock so worker threads never fight over commits."""
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self.lock = threading.RLock()
        self.init_schema()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            # Two processes share this database (the background agent writes,
            # the window reads and writes small acknowledgements). WAL plus a
            # generous busy timeout means neither ever sees a raw lock error.
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    def init_schema(self):
        with self.lock:
            c = self.conn()
            had_fts = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='messages_fts'").fetchone() is not None
            c.executescript(_SCHEMA)
            self._ensure_column(
                c, "chats", "hidden", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(c, "messages", "source_rowid", "INTEGER")
            self._ensure_column(
                c, "messages", "first_seen_ms", "INTEGER NOT NULL DEFAULT 0")
            added_delivery_marker = self._ensure_column(
                c, "messages", "delivery_event_recorded",
                "INTEGER NOT NULL DEFAULT 0")
            if added_delivery_marker:
                # Every row predating this durable marker is already part of
                # the user's established cache. Do not recreate old alerts
                # merely because a completed ledger row was once pruned.
                c.execute(
                    "UPDATE messages SET delivery_event_recorded=1")
            # ROWIDs are unique inside one Mac chat.db generation.  Keep this
            # non-unique locally so a rebuilt Mac database can be re-indexed by
            # GUID without an upgrade-time collision.
            c.execute("DROP INDEX IF EXISTS idx_msg_source_rowid")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_source_rowid "
                "ON messages(source_rowid) WHERE source_rowid IS NOT NULL")
            if not had_fts:
                # External-content FTS tables do not automatically index rows
                # that predate their creation during an upgrade.
                c.execute(
                    "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            c.commit()

    @staticmethod
    def _ensure_column(c: sqlite3.Connection, table: str, column: str,
                       declaration: str) -> bool:
        """Small explicit migrations for databases created by older builds."""
        names = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in names:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            return True
        return False

    def write(self, sql: str, params=()):
        with self.lock:
            c = self.conn()
            cur = c.execute(sql, params)
            c.commit()
            return cur

    def txn(self, fn):
        """Run fn(conn) inside a single committed transaction."""
        with self.lock:
            c = self.conn()
            try:
                result = fn(c)
                c.commit()
                return result
            except Exception:
                c.rollback()
                raise

    def query(self, sql: str, params=()):
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        return self.conn().execute(sql, params).fetchone()
