CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chats (
  guid           TEXT PRIMARY KEY,
  display_name   TEXT,
  is_group       INTEGER NOT NULL DEFAULT 0,
  participants   TEXT,
  last_activity  INTEGER,
  unread         INTEGER NOT NULL DEFAULT 0,
  archived       INTEGER NOT NULL DEFAULT 0,
  hidden         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS handles (
  address       TEXT PRIMARY KEY,
  norm          TEXT,
  display_name  TEXT
);
CREATE INDEX IF NOT EXISTS idx_handles_norm ON handles(norm);

CREATE TABLE IF NOT EXISTS messages (
  guid                    TEXT PRIMARY KEY,
  source_rowid            INTEGER,
  chat_guid               TEXT NOT NULL,
  sender_address          TEXT,
  is_from_me              INTEGER NOT NULL DEFAULT 0,
  text                    TEXT,
  subject                 TEXT,
  service                 TEXT,
  date_created            INTEGER NOT NULL,
  date_delivered          INTEGER,
  date_read               INTEGER,
  is_edited               INTEGER NOT NULL DEFAULT 0,
  is_retracted            INTEGER NOT NULL DEFAULT 0,
  thread_originator_guid  TEXT,
  associated_guid         TEXT,
  associated_type         INTEGER,
  item_type               INTEGER NOT NULL DEFAULT 0,
  error                   INTEGER NOT NULL DEFAULT 0,
  raw                     TEXT,
  first_seen_ms           INTEGER NOT NULL DEFAULT 0,
  delivery_event_recorded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_chat_date ON messages(chat_guid, date_created);
CREATE INDEX IF NOT EXISTS idx_msg_assoc ON messages(associated_guid);
CREATE INDEX IF NOT EXISTS idx_msg_read ON messages(chat_guid, is_from_me, date_read);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='rowid', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, coalesce(new.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text)
  VALUES ('delete', old.rowid, coalesce(old.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text)
  VALUES ('delete', old.rowid, coalesce(old.text, ''));
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, coalesce(new.text, ''));
END;

CREATE TABLE IF NOT EXISTS attachments (
  guid          TEXT PRIMARY KEY,
  message_guid  TEXT NOT NULL,
  mime_type     TEXT,
  file_name     TEXT,
  total_bytes   INTEGER,
  width         INTEGER,
  height        INTEGER,
  local_path    TEXT,
  state         TEXT NOT NULL DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_att_msg ON attachments(message_guid);

CREATE TABLE IF NOT EXISTS outbox (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  temp_guid    TEXT NOT NULL UNIQUE,
  chat_guid    TEXT NOT NULL,
  text         TEXT,
  attach_path  TEXT,
  created_ts   INTEGER NOT NULL,
  state        TEXT NOT NULL DEFAULT 'queued',
  server_guid  TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
  chat_guid      TEXT PRIMARY KEY,
  oldest_synced  INTEGER,
  backfill_done  INTEGER NOT NULL DEFAULT 0
);

-- Incoming side effects are durable and independent of whichever transport
-- (socket, poll, or startup catch-up) wins the insert race.  A crash before a
-- popup is actually shown leaves notification_done=0 for replay next start.
CREATE TABLE IF NOT EXISTS delivery_events (
  message_guid       TEXT PRIMARY KEY,
  chat_guid          TEXT NOT NULL,
  first_seen_ms      INTEGER NOT NULL,
  unread_done        INTEGER NOT NULL DEFAULT 0,
  notification_done  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_delivery_pending
  ON delivery_events(notification_done, first_seen_ms);
CREATE INDEX IF NOT EXISTS idx_delivery_unread_pending
  ON delivery_events(unread_done, first_seen_ms);

-- The in-app notification center. Every user-facing alert (message,
-- wake, repair, connection change, test) lands here so the bell can show
-- what happened, including alerts raised while no one was watching.
-- Rows are soft-hidden, never mutated, and pruned by feed_prune().
CREATE TABLE IF NOT EXISTS feed (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL,
  body          TEXT,
  chat_guid     TEXT,
  message_guid  TEXT,
  created_ms    INTEGER NOT NULL,
  seen          INTEGER NOT NULL DEFAULT 0,
  hidden        INTEGER NOT NULL DEFAULT 0
);
-- Message alerts deduplicate durably: repeated ledger sweeps and popup
-- retries may record the same message blindly and only one row exists.
CREATE UNIQUE INDEX IF NOT EXISTS idx_feed_message_unique
  ON feed(message_guid) WHERE message_guid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_feed_recent ON feed(hidden, created_ms);

-- A malformed server row must not wedge the ROWID cursor or disappear
-- silently.  It is quarantined here and retried from an authoritative query.
CREATE TABLE IF NOT EXISTS sync_failures (
  source_rowid     INTEGER PRIMARY KEY,
  guid             TEXT,
  raw              TEXT NOT NULL,
  error            TEXT,
  attempts         INTEGER NOT NULL DEFAULT 1,
  last_attempt_ms  INTEGER NOT NULL
);
