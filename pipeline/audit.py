"""Human audit of the abstract screener's exclusions.

    python -m pipeline.audit merge decisions.json   # copy decisions from the audit page into review_queue.csv
    python -m pipeline.audit apply                  # apply audited decisions to the records, write the report

review_queue.csv columns filled by a person:
    human_decision  confirm   the exclusion is right, code included
                    recode    the record is excluded, but under human_code instead
                    fulltext  the screener was wrong: the record goes to full-text review
    human_code      E1-E10, only for recode
    human_notes     free text

apply is idempotent. A 'fulltext' record becomes abstract_include with decided_by 'human:audit'.
A 'recode' record keeps status excluded with the new code. 'confirm' changes nothing in the record.
The report (data/screening/audit_report.json) gives the screener's false-exclusion rate on the
random audit sample, which is the estimate of how many unaudited excludes are wrong.
"""
import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "screening"
QUEUE = OUT / "review_queue.csv"
REPORT = OUT / "audit_report.json"
DB = ROOT / "data" / "records.sqlite"
FIELDS = ["record_id", "screener_decision", "exclusion_code", "reason", "title", "rationale",
          "human_decision", "human_code", "human_notes"]
DECISIONS = {"confirm", "recode", "fulltext"}
CODES = {f"E{i}" for i in range(1, 11)}


def read_queue(path=QUEUE):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_queue(rows, path=QUEUE):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in FIELDS} for r in rows)


def valid(decision, code):
    if decision not in DECISIONS:
        return False
    return code in CODES if decision == "recode" else True


def merge(rows, decisions):
    """Copy page decisions into queue rows. The page is the newer source, so it overwrites."""
    by_id = {str(r["record_id"]): r for r in rows}
    n, skipped = 0, []
    for d in decisions:
        rid = str(d.get("record_id"))
        dec, code = (d.get("decision") or "").strip(), (d.get("code") or "").strip().upper()
        if rid not in by_id or not valid(dec, code):
            skipped.append(rid)
            continue
        r = by_id[rid]
        r["human_decision"], r["human_code"] = dec, code if dec == "recode" else ""
        r["human_notes"] = (d.get("notes") or "").strip()
        n += 1
    return n, skipped


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, mid - half), 4), round(min(1.0, mid + half), 4)


def report(rows):
    groups = {
        "random_sample": lambda r: "random audit sample" in r["reason"],
        "quote_not_found": lambda r: "quote not found" in r["reason"] and "random audit sample" not in r["reason"],
        "injection": lambda r: "prompt injection" in r["reason"],
    }
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "queue": len(rows), "audited": sum(1 for r in rows if r["human_decision"]), "groups": {}}
    for name, test in groups.items():
        g = [r for r in rows if test(r)]
        done = [r for r in g if r["human_decision"]]
        wrong = sum(1 for r in done if r["human_decision"] == "fulltext")
        recoded = sum(1 for r in done if r["human_decision"] == "recode")
        lo, hi = wilson(wrong, len(done))
        out["groups"][name] = {"total": len(g), "audited": len(done), "sent_to_fulltext": wrong,
                               "recoded": recoded, "false_exclusion_rate": round(wrong / len(done), 4) if done else None,
                               "ci95": [lo, hi]}
    return out


def apply(con, rows):
    changed = {"to_fulltext": 0, "recoded": 0}
    for r in rows:
        rid, dec = int(r["record_id"]), r["human_decision"]
        if not valid(dec, r["human_code"]):
            continue
        cur = con.execute("SELECT status, exclusion_code, decided_by FROM records WHERE record_id=?", (rid,)).fetchone()
        if cur is None:
            continue
        if dec == "fulltext" and cur["status"] == "excluded":
            con.execute("UPDATE records SET status='abstract_include', exclusion_code=NULL, decided_by='human:audit' "
                        "WHERE record_id=?", (rid,))
            changed["to_fulltext"] += 1
        elif dec == "recode" and cur["exclusion_code"] != r["human_code"] and (
                cur["status"] == "excluded" or cur["decided_by"] == "human:audit"):
            con.execute("UPDATE records SET status='excluded', exclusion_code=?, decided_by='human:audit' "
                        "WHERE record_id=?", (r["human_code"], rid))
            changed["recoded"] += 1
        elif dec == "confirm" and cur["decided_by"] == "human:audit" and (
                cur["status"] != "excluded" or cur["exclusion_code"] != r["exclusion_code"]):
            # an earlier audit call was reversed: restore the screener's exclusion
            con.execute("UPDATE records SET status='excluded', exclusion_code=? WHERE record_id=?",
                        (r["exclusion_code"] or None, rid))
            changed["restored"] = changed.get("restored", 0) + 1
    con.commit()
    return changed


GROUPS = [("injection", "Possible prompt injection"), ("random", "Random audit sample"),
          ("quote", "Quote not found")]


def group_of(reason):
    if "prompt injection" in reason:
        return "injection"
    return "random" if "random audit sample" in reason else "quote"


def page_items(rows):
    """Queue rows joined to their records and screener decisions, in audit order.

    Order: possible injections, then the random sample, then quote-not-found. Within a group the
    order is a fixed shuffle, so any first stretch of the random sample is itself a random sample.
    """
    import glob
    import random
    recs, decs = {}, {}
    for f in sorted(glob.glob(str(ROOT / "data" / "records-*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            recs[r["record_id"]] = r
    for f in sorted(glob.glob(str(OUT / "abstract_decisions-*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            d = json.loads(line)
            decs[d["record_id"]] = d
    items = []
    for row in rows:
        rid = int(row["record_id"])
        rec, dec = recs.get(rid, {}), decs.get(rid, {})
        items.append({"id": rid, "g": group_of(row["reason"]), "why": row["reason"],
                      "t": rec.get("title") or row["title"], "ab": rec.get("abstract") or "",
                      "v": rec.get("venue") or "", "y": rec.get("year"),
                      "u": f"https://doi.org/{rec['doi']}" if rec.get("doi") else rec.get("url") or "",
                      "c": row["exclusion_code"], "r": row["rationale"], "q": dec.get("quote") or "",
                      "qv": bool(dec.get("quote_verified"))})
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    random.Random(2026).shuffle(items)
    items.sort(key=lambda x: order[x["g"]])
    return items


def build_page(rows, out):
    tpl = (ROOT / "site_src" / "audit_template.html").read_text(encoding="utf-8")
    data = json.dumps(page_items(rows), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    data = data.replace("\ufffd", "\\ufffd")   # keep source-data replacement characters as JSON escapes
    Path(out).write_text(tpl.replace("{{DATA}}", data), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["merge", "apply", "page"])
    ap.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    if not QUEUE.exists():
        print(json.dumps({"queue": 0}))
        return
    rows = read_queue()
    if args.mode == "page":
        build_page(rows, args.file or "_site/audit.html")
        return
    if args.mode == "merge":
        if not args.file:
            raise SystemExit("merge needs a JSON file of decisions")
        n, skipped = merge(rows, json.loads(Path(args.file).read_text(encoding="utf-8")))
        write_queue(rows)
        print(json.dumps({"merged": n, "skipped": skipped}))
        return
    con = db.connect(DB)
    changed = apply(con, rows)
    db.save(con, DB)
    from .search import export_csv
    export_csv(con, DB.with_suffix(".csv"))
    rep = report(rows)
    rep["applied"] = changed
    REPORT.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps(rep))


if __name__ == "__main__":
    main()
