-- reqable-mcp local SQLite cache schema (v1).
-- This DB is a query index built from Reqable's LMDB; the LMDB is the truth.
-- Body content is NOT stored here — fetched on demand via dbData / rest/{uid}.bin.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -8000;
PRAGMA busy_timeout = 5000;

-- --------------------------------------------------------------------
-- One row per captured request/response pair.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS captures (
  uid           TEXT    PRIMARY KEY,         -- Reqable's UUID, e.g. "0e65fcea-..."
  ob_id         INTEGER UNIQUE,              -- ObjectBox internal id (sync cursor)
  ts            INTEGER NOT NULL,            -- timestamp, unix ms
  scheme        TEXT,
  host          TEXT,
  port          INTEGER,
  url           TEXT,
  path          TEXT,
  method        TEXT,
  status        INTEGER,
  protocol      TEXT,
  req_mime      TEXT,
  res_mime      TEXT,
  app_name      TEXT,
  app_id        TEXT,
  app_path      TEXT,
  req_body_size INTEGER,
  res_body_size INTEGER,
  rtt_ms        INTEGER,
  comment       TEXT,
  ssl_bypassed  INTEGER,
  has_error     INTEGER,
  source        TEXT    NOT NULL DEFAULT 'lmdb',  -- 'lmdb' | 'hook' (Phase 2)
  raw_summary   TEXT                              -- "<METHOD> <url> -> <status>"
);

CREATE INDEX IF NOT EXISTS idx_captures_ts             ON captures(ts DESC);
CREATE INDEX IF NOT EXISTS idx_captures_host_ts        ON captures(host, ts DESC);
CREATE INDEX IF NOT EXISTS idx_captures_app_ts         ON captures(app_name, ts DESC);
CREATE INDEX IF NOT EXISTS idx_captures_method_status  ON captures(method, status);

-- --------------------------------------------------------------------
-- Lightweight FTS index on URL + summary, NOT body (kept small + fast).
-- --------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
  url, summary,
  content='captures',
  content_rowid='rowid',
  tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS captures_ai AFTER INSERT ON captures BEGIN
  INSERT INTO captures_fts(rowid, url, summary)
  VALUES (new.rowid, new.url, new.raw_summary);
END;
CREATE TRIGGER IF NOT EXISTS captures_ad AFTER DELETE ON captures BEGIN
  INSERT INTO captures_fts(captures_fts, rowid, url, summary)
  VALUES ('delete', old.rowid, old.url, old.raw_summary);
END;
CREATE TRIGGER IF NOT EXISTS captures_au AFTER UPDATE ON captures BEGIN
  INSERT INTO captures_fts(captures_fts, rowid, url, summary)
  VALUES ('delete', old.rowid, old.url, old.raw_summary);
  INSERT INTO captures_fts(rowid, url, summary)
  VALUES (new.rowid, new.url, new.raw_summary);
END;

-- --------------------------------------------------------------------
-- Per-source sync cursor (LMDB last seen ob_id, etc.).
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
  source       TEXT    PRIMARY KEY,
  last_ob_id   INTEGER NOT NULL DEFAULT 0,
  last_ts      INTEGER NOT NULL DEFAULT 0,
  last_run_ts  INTEGER NOT NULL DEFAULT 0,
  schema_hash  TEXT
);

-- --------------------------------------------------------------------
-- Phase 2 placeholder: rule definitions written by tag/modify tools.
-- Columns kept generic enough that addons.py can read JSON-ified rows.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
  rule_id     TEXT    PRIMARY KEY,
  type        TEXT    NOT NULL,            -- tag | modify | mock | block
  enabled     INTEGER NOT NULL DEFAULT 1,
  spec_json   TEXT    NOT NULL,
  created_ts  INTEGER,
  expires_ts  INTEGER,
  hits        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rules_type_enabled ON rules(type, enabled);
