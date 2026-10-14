"""Baseline and weekly abstract screening through the Message Batches API (half price, results within 24 hours).

    python -m pipeline.batch submit     # send every record at status 'found' to the screener
    python -m pipeline.batch poll       # collect finished batches and apply the decisions

submit marks records 'screening' and saves the batch ids in data/screening/batches.json.
poll applies results:
    include -> status 'abstract_include'     unsure -> 'abstract_unsure'
    exclude -> status 'excluded' with the screener's exclusion code
    errored, expired, or canceled -> back to 'found', picked up by the next submit
Every decision is appended to data/screening/abstract_decisions-*.jsonl, and records needing a
person go to data/screening/review_queue.csv.
"""
import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import db, screen
from .search import export_csv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "screening"
DB = ROOT / "data" / "records.sqlite"
MODEL = "claude-haiku-5-5"
CHUNK = 5000
SAMPLE_RATE = 0.10          # weekly runs: share of screener excludes sent to a person (protocol section 4)
BASELINE_AUDIT = 300        # baseline: fixed audit sample of screener excludes (PI decision 2026-10-13)
API = "https://api.anthropic.com/v1/messages/batches"
STATUS = {"include": "abstract_include", "unsure": "abstract_unsure"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def headers(key):
    return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}


def load_batches():
    f = OUT / "batches.json"
    return json.loads(f.read_text()) if f.exists() else []


def save_batches(b):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "batches.json").write_text(json.dumps(b, indent=1))


# Some models reject a forced tool choice; the prompt already asks for the tool, so "auto" is enough there.
AUTO_TOOL_MODELS = {"claude-sonnet-5-5"}


def tool_choice(model):
    return {"type": "auto"} if model in AUTO_TOOL_MODELS else {"type": "tool", "name": "record_decision"}


def request_for(rec, model=MODEL):
    fixed, record = screen.build_prompt(rec)
    return {"custom_id": f"r{rec['record_id']}",
            "params": {"model": model, "max_tokens": 1200,
                       "system": [{"type": "text", "text": fixed, "cache_control": {"type": "ephemeral"}}],
                       "tools": [screen.TOOL], "tool_choice": tool_choice(model),
                       "messages": [{"role": "user", "content": record}]}}


def submit(key, post=None, limit=None):
    post = post or (lambda body: requests.post(API, headers=headers(key), json=body, timeout=600))
    con = db.connect(DB)
    recs = [dict(r) for r in con.execute("SELECT record_id, title, abstract, venue, year FROM records "
                                         "WHERE status='found' ORDER BY record_id")]
    recs = recs[:limit] if limit else recs
    batches = load_batches()
    for i in range(0, len(recs), CHUNK):
        part = recs[i:i + CHUNK]
        r = post({"requests": [request_for(x) for x in part]})
        if r.status_code != 200:
            raise SystemExit(f"Batch create failed: HTTP {r.status_code} {r.text[:300]}")
        b = r.json()
        batches.append({"id": b["id"], "created": now(), "model": MODEL, "prompt_version": screen.PROMPT_VERSION,
                        "n": len(part), "first_record": part[0]["record_id"], "last_record": part[-1]["record_id"],
                        "state": "submitted"})
        con.executemany("UPDATE records SET status='screening' WHERE record_id=?", [(x["record_id"],) for x in part])
        con.commit()
        save_batches(batches)
    db.save(con, DB)
    export_csv(con, DB.with_suffix(".csv"))
    return {"submitted": len(recs), "batches": [b["id"] for b in batches if b["state"] == "submitted"]}


SECOND_MODEL = "claude-sonnet-5-5"
REVIEW_FIELDS = ["record_id", "screener_decision", "exclusion_code", "reason", "title", "rationale",
                 "second_screen", "second_decision", "second_code", "second_rationale",
                 "human_decision", "human_code", "human_notes"]


def read_review():
    with open(OUT / "review_queue.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in REVIEW_FIELDS:
            r.setdefault(k, "")
    return rows


def write_review(rows):
    with open(OUT / "review_queue.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in REVIEW_FIELDS} for r in rows)


def submit_second(key, post=None):
    """Send open review rows whose quote did not verify to a second, stronger screener."""
    post = post or (lambda body: requests.post(API, headers=headers(key), json=body, timeout=600))
    con = db.connect(DB)
    rows = read_review()
    ids = [int(r["record_id"]) for r in rows if "quote not found" in r["reason"]
           and not r["human_decision"] and not r["second_screen"]]
    if not ids:
        return {"submitted": 0}
    recs = [dict(r) for r in con.execute(f"SELECT record_id, title, abstract, venue, year FROM records "
                                         f"WHERE record_id IN ({','.join(map(str, ids))})")]
    r = post({"requests": [request_for(x, SECOND_MODEL) for x in recs]})
    if r.status_code != 200:
        raise SystemExit(f"Batch create failed: HTTP {r.status_code} {r.text[:300]}")
    batches = load_batches()
    batches.append({"id": r.json()["id"], "kind": "second", "created": now(), "model": SECOND_MODEL,
                    "prompt_version": screen.PROMPT_VERSION, "n": len(recs), "state": "submitted"})
    save_batches(batches)
    for row in rows:
        if int(row["record_id"]) in ids:
            row["second_screen"] = "submitted"
    write_review(rows)
    return {"submitted": len(recs), "model": SECOND_MODEL}


def apply_second(lines, batch):
    """Agreement on exclude closes the review row. Anything else stays open for a person."""
    rows = {int(r["record_id"]): r for r in read_review()}
    out = {"agree_exclude": 0, "disagree": 0, "failed": 0}
    log = []
    for line in lines:
        res = json.loads(line)
        rid = int(res["custom_id"][1:])
        row = rows.get(rid)
        if row is None:
            continue
        try:
            if res["result"]["type"] != "succeeded":
                raise ValueError(f"{res['result']['type']}: {json.dumps(res['result'].get('error'))[:300]}")
            msg = res["result"]["message"]
            tool = next((b["input"] for b in msg["content"] if b.get("type") == "tool_use"), None)
            dec = screen.parse(tool if tool is not None else "".join(b.get("text", "") for b in msg["content"]))
        except Exception as e:
            row["second_screen"] = ""          # resubmitted next time
            out["failed"] += 1
            out.setdefault("errors", {})
            k = f"{type(e).__name__}: {e}"[:200]
            out["errors"][k] = out["errors"].get(k, 0) + 1
            continue
        row.update(second_decision=dec["decision"], second_code=dec.get("exclusion_code") or "",
                   second_rationale=dec.get("rationale", ""))
        if dec["decision"] == "exclude":
            row["second_screen"] = "agree_exclude"
            out["agree_exclude"] += 1
        else:
            row["second_screen"] = "disagree"
            out["disagree"] += 1
        log.append({"record_id": rid, "batch": batch["id"], "model": batch["model"],
                    "prompt_version": batch["prompt_version"], "decided": now(), **dec,
                    "usage": msg.get("usage")})
    write_review(list(rows.values()))
    with open(OUT / "second_screen_decisions.jsonl", "a", encoding="utf-8") as f:
        for r in log:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out


def apply_results(con, lines, batch, rng):
    out = {"include": 0, "unsure": 0, "exclude": 0, "failed": 0, "to_review": 0}
    log, review = [], []
    recs = {r["record_id"]: dict(r) for r in con.execute(
        "SELECT record_id, title, abstract, venue FROM records WHERE status='screening'")}
    for line in lines:
        res = json.loads(line)
        rid = int(res["custom_id"][1:])
        rec = recs.get(rid)
        if rec is None:
            continue
        row = {"record_id": rid, "batch": batch["id"], "model": batch["model"],
               "prompt_version": batch["prompt_version"], "decided": now()}
        try:
            if res["result"]["type"] != "succeeded":
                raise ValueError(res["result"]["type"])
            msg = res["result"]["message"]
            tool = next(b["input"] for b in msg["content"] if b.get("type") == "tool_use")
            dec = screen.parse(tool)
            row.update(dec, record_id=rid, usage=msg.get("usage"))
        except Exception as e:
            con.execute("UPDATE records SET status='found' WHERE record_id=?", (rid,))
            out["failed"] += 1
            continue
        source = screen.norm(f"{rec['title']} {rec['venue'] or ''} {rec['abstract'] or ''}")
        row["quote_verified"] = bool(row.get("quote")) and screen.norm(row["quote"]) in source
        d = row["decision"]
        out[d] += 1
        who = f"{batch['prompt_version']}/{batch['model']}"
        if d == "exclude":
            con.execute("UPDATE records SET status='excluded', exclusion_code=?, decided_by=? WHERE record_id=?",
                        (row.get("exclusion_code"), who, rid))
            reasons = []
            if not row["quote_verified"]:
                reasons.append("quote not found in record")
            if not row.get("exclusion_code"):
                reasons.append("no exclusion code")
            if batch.get("audit", "rate") == "rate" and rng.random() < SAMPLE_RATE:
                reasons.append("random audit sample")
        else:
            con.execute("UPDATE records SET status=?, decided_by=? WHERE record_id=?", (STATUS[d], who, rid))
            reasons = []
        passage = screen.injection_passage(f"{rec['title']} {rec['abstract'] or ''}")
        if passage:
            reasons.append(f"injection pattern in text: ...{passage}...")
        elif row.get("injection_suspected"):
            reasons.append("screener suspected injection; no known pattern found in text")
        if reasons:
            review.append({"record_id": rid, "screener_decision": d, "exclusion_code": row.get("exclusion_code") or "",
                           "reason": "; ".join(reasons), "title": rec["title"], "rationale": row.get("rationale", ""),
                           "human_decision": "", "human_code": "", "human_notes": ""})
        log.append(row)
    out["to_review"] = len(review)
    return out, log, review


def append_jsonl(rows):
    for r in rows:
        n = (r["record_id"] - 1) // CHUNK
        with open(OUT / f"abstract_decisions-{n:04d}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_review(rows):
    existing = read_review() if (OUT / "review_queue.csv").exists() else []
    write_review(existing + rows)


def poll(key, get=None):
    get = get or (lambda url: requests.get(url, headers=headers(key), timeout=600))
    batches = load_batches()
    open_ = [b for b in batches if b["state"] == "submitted"]
    if not open_:
        return {"open_batches": 0}
    con = db.connect(DB)
    summary = {}
    for b in open_:
        info = get(f"{API}/{b['id']}").json()
        b["counts"] = info.get("request_counts")
        if info.get("processing_status") != "ended":
            summary[b["id"]] = {"status": info.get("processing_status"), "counts": b["counts"]}
            continue
        lines = [l for l in get(info["results_url"]).text.splitlines() if l.strip()]
        if b.get("kind") == "second":
            out = apply_second(lines, b)
        else:
            rng = random.Random(b["id"])
            out, log, review = apply_results(con, lines, b, rng)
            append_jsonl(log)
            append_review(review)
        b.update(state="applied", applied=now(), result=out)
        summary[b["id"]] = out
        con.commit()
        save_batches(batches)
    save_batches(batches)
    db.save(con, DB)
    export_csv(con, DB.with_suffix(".csv"))
    summary["status_counts"] = dict(con.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["submit", "second", "poll"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    OUT.mkdir(parents=True, exist_ok=True)
    res = {"submit": lambda: submit(key, limit=args.limit), "second": lambda: submit_second(key),
           "poll": lambda: poll(key)}[args.mode]()
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
