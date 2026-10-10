"""Records database. One row per candidate study, with a status the later stages advance."""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id   INTEGER PRIMARY KEY,
    doi         TEXT UNIQUE,
    title_key   TEXT,
    title       TEXT,
    abstract    TEXT,
    authors     TEXT,
    year        INTEGER,
    pub_date    TEXT,
    venue       TEXT,
    pub_type    TEXT,
    url         TEXT,
    sources     TEXT NOT NULL,          -- JSON: {source: source_id}
    status      TEXT NOT NULL DEFAULT 'found',
    first_run   INTEGER,
    first_seen  TEXT
);
CREATE INDEX IF NOT EXISTS idx_title_key ON records(title_key);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY, started TEXT, mode TEXT, since TEXT, summary TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def start_run(con, mode, since):
    cur = con.execute("INSERT INTO runs(started, mode, since) VALUES (?,?,?)", (now(), mode, since))
    return cur.lastrowid


def finish_run(con, run_id, summary):
    con.execute("UPDATE runs SET summary=? WHERE run_id=?", (json.dumps(summary), run_id))
    con.commit()


def upsert(con, rec, run_id):
    """Insert a record or merge it into an existing one. Returns 'new' or 'duplicate'."""
    row = None
    if rec["doi"]:
        row = con.execute("SELECT * FROM records WHERE doi=?", (rec["doi"],)).fetchone()
    if row is None and rec["title_key"]:
        # Title match only merges when the DOIs do not conflict.
        for cand in con.execute("SELECT * FROM records WHERE title_key=?", (rec["title_key"],)):
            if not (cand["doi"] and rec["doi"] and cand["doi"] != rec["doi"]):
                row = cand
                break
    if row is None:
        con.execute(
            "INSERT INTO records(doi,title_key,title,abstract,authors,year,pub_date,venue,pub_type,url,"
            "sources,first_run,first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["doi"], rec["title_key"], rec["title"], rec["abstract"], json.dumps(rec["authors"]),
             rec["year"], rec["pub_date"], rec["venue"], rec["pub_type"], rec["url"],
             json.dumps({rec["source"]: rec["source_id"]}), run_id, now()))
        return "new"
    sources = json.loads(row["sources"])
    sources.setdefault(rec["source"], rec["source_id"])
    # Fill gaps only. Never overwrite a value or touch status.
    fills = {k: rec[k] for k in ("doi", "abstract", "venue", "pub_date", "url", "pub_type")
             if not row[k] and rec[k]}
    sets = ", ".join(f"{k}=?" for k in fills)
    con.execute(f"UPDATE records SET sources=?{', ' + sets if sets else ''} WHERE record_id=?",
                (json.dumps(sources), *fills.values(), row["record_id"]))
    return "duplicate"
