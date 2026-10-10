"""Records database. One row per candidate study, with a status the later stages advance."""
import json
import sqlite3
from pathlib import Path
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


RECORD_COLS = ["record_id", "doi", "title_key", "title", "abstract", "authors", "year", "pub_date", "venue",
               "pub_type", "url", "sources", "status", "first_run", "first_seen", "linked_to", "link_note", "exclusion_code", "decided_by"]
RUN_COLS = ["run_id", "started", "mode", "since", "summary"]


def connect(path):
    """Open the working database. It is rebuilt from the text files in git when it is missing or empty."""
    path = Path(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(records)")}
    for col, kind in (("linked_to", "INTEGER"), ("link_note", "TEXT"), ("exclusion_code", "TEXT"),
                      ("decided_by", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE records ADD COLUMN {col} {kind}")
    if con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0:
        for table, cols, pattern in (("records", RECORD_COLS, "records-*.jsonl"), ("runs", RUN_COLS, "runs.jsonl")):
            for f in sorted(path.parent.glob(pattern)):
                rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
                con.executemany(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                [[r.get(c) for c in cols] for r in rows])
    con.commit()
    return con


CHUNK = 5000        # records per text file, which keeps each file well under GitHub's size warning


def save(con, path):
    """Write the database as text, one record per line, so git stores weekly changes as small diffs.

    Records are split into files of CHUNK by record_id, so new records only touch the last file.
    """
    con.commit()
    path = Path(path)
    for old in path.parent.glob("records-*.jsonl"):
        old.unlink()
    files = {}
    for r in con.execute(f"SELECT {','.join(RECORD_COLS)} FROM records ORDER BY record_id"):
        n = (r["record_id"] - 1) // CHUNK
        if n not in files:
            files[n] = open(path.with_name(f"records-{n:04d}.jsonl"), "w", encoding="utf-8")
        files[n].write(json.dumps(dict(r), ensure_ascii=False) + "\n")
    for f in files.values():
        f.close()
    with open(path.with_name("runs.jsonl"), "w", encoding="utf-8") as f:
        for r in con.execute(f"SELECT {','.join(RUN_COLS)} FROM runs ORDER BY run_id"):
            f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")


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
