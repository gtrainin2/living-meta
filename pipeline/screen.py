"""Stage 2, title and abstract screening with the screener prompt.

    python -m pipeline.screen calibrate [--models claude-sonnet-5-5,claude-haiku-5-5]

calibrate runs the screener on the calibration records that survive the publication-type rules,
writes every decision to data/calibration/screener_<model>.jsonl, and writes the agreement with
the human codes to data/calibration/screener_report.json. It needs ANTHROPIC_API_KEY.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
CAL = ROOT / "data" / "calibration"
PROMPT_VERSION = "screener-0.2"
API = "https://api.anthropic.com/v1/messages"


def build_prompt(rec, stage="abstract"):
    tmpl = (ROOT / "prompts" / "screener.md").read_text(encoding="utf-8").split("-->", 1)[1].lstrip()
    elig = (ROOT / "prompts" / "eligibility.md").read_text(encoding="utf-8")
    body = (f"Title: {rec['title']}\nVenue: {rec['venue'] or 'not given'}\nYear: {rec['year'] or 'not given'}\n"
            f"Abstract: {rec['abstract'] or '[no abstract in the index]'}")
    return (tmpl.replace("{{eligibility_table_and_exclusion_codes}}", elig).replace("{{stage}}", stage)
            .replace("{{record_id}}", str(rec["record_id"])).replace("{{record}}", body))


def call_model(prompt, model, key, tries=6):
    import requests
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": model, "max_tokens": 1200, "temperature": 0,
               "messages": [{"role": "user", "content": prompt}]}
    for i in range(tries):
        r = requests.post(API, headers=headers, json=payload, timeout=120)
        if r.status_code in (429, 500, 502, 503, 529):
            time.sleep(min(60, 3 * 2 ** i))
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"HTTP {r.status_code}: the API rejected the key")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        js = r.json()
        return "".join(b.get("text", "") for b in js["content"]), js.get("usage", {})
    raise RuntimeError(f"gave up after {tries} tries, last HTTP {r.status_code}")


def parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    out = json.loads(m.group(0))
    if out.get("decision") not in ("include", "exclude", "unsure"):
        raise ValueError(f"bad decision {out.get('decision')!r}")
    return out


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()


def screen_one(rec, model, key, call=call_model):
    prompt = build_prompt(rec)
    row = {"record_id": rec["record_id"], "model": model, "prompt_version": PROMPT_VERSION}
    for attempt in (1, 2):
        try:
            text, usage = call(prompt, model, key)
            out = parse(text)
            row.update(out, record_id=rec["record_id"], usage=usage, error=None)
            source = norm(f"{rec['title']} {rec['abstract'] or ''}")
            row["quote_verified"] = bool(out.get("quote")) and norm(out["quote"]) in source
            return row
        except Exception as e:  # malformed reply gets one retry, then goes to human review
            row.update(decision=None, error=f"{type(e).__name__}: {e}")
    return row


def calibration_records(con):
    codes = {int(r["record_id"]): r for r in csv.DictReader(open(CAL / "abstract_human_codes.csv"))}
    ids = ",".join(map(str, codes))
    recs = con.execute(f"SELECT record_id, title, abstract, venue, year, status FROM records "
                       f"WHERE record_id IN ({ids})").fetchall()
    return [dict(r) for r in recs if r["status"] == "found"], codes


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((c - h) / d, 3), round((c + h) / d, 3)]


def score(rows, codes):
    """Human include is the reference. The screener 'passes' a record when it says include or unsure."""
    t = {"pass_inc": 0, "fail_inc": 0, "pass_exc": 0, "fail_exc": 0}
    misses, code_agree, code_n, errors, quotes = [], 0, 0, 0, 0
    for r in rows:
        h = codes[r["record_id"]]
        if h["decision"] not in ("include", "exclude"):
            continue
        if r.get("decision") is None:
            errors += 1
            continue
        passed = r["decision"] in ("include", "unsure")
        key = ("pass_" if passed else "fail_") + ("inc" if h["decision"] == "include" else "exc")
        t[key] += 1
        quotes += bool(r.get("quote_verified"))
        if h["decision"] == "include" and not passed:
            misses.append({"record_id": r["record_id"], "screener_code": r.get("exclusion_code"),
                           "rationale": r.get("rationale")})
        if h["decision"] == "exclude" and r["decision"] == "exclude":
            code_n += 1
            code_agree += (r.get("exclusion_code") or "") == h["exclusion_code"]
    inc, exc = t["pass_inc"] + t["fail_inc"], t["pass_exc"] + t["fail_exc"]
    n = inc + exc
    po = (t["pass_inc"] + t["fail_exc"]) / n if n else None
    pe = (((t["pass_inc"] + t["pass_exc"]) * inc + (t["fail_inc"] + t["fail_exc"]) * exc) / (n * n)) if n else None
    kappa = round((po - pe) / (1 - pe), 3) if n and pe < 1 else None
    decided = [r for r in rows if r.get("decision")]
    return {
        "records_scored": n, "human_includes": inc, "human_excludes": exc, "errors": errors,
        "table": t,
        "sensitivity": round(t["pass_inc"] / inc, 3) if inc else None, "sensitivity_95ci": wilson(t["pass_inc"], inc),
        "specificity": round(t["fail_exc"] / exc, 3) if exc else None, "specificity_95ci": wilson(t["fail_exc"], exc),
        "kappa_pass_vs_exclude": kappa,
        "exclusion_code_agreement": round(code_agree / code_n, 3) if code_n else None,
        "decisions": {d: sum(1 for r in decided if r["decision"] == d) for d in ("include", "unsure", "exclude")},
        "quotes_verified": f"{quotes}/{len(decided)}",
        "missed_human_includes": misses,
    }


def calibrate(models, workers=4, call=call_model, db_path=ROOT / "data" / "records.sqlite"):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if call is call_model:
        if not key:
            raise SystemExit("ANTHROPIC_API_KEY is not set")
        if not key.startswith("sk-ant-"):
            raise SystemExit("ANTHROPIC_API_KEY does not look like an Anthropic key (it should start with sk-ant-)")
        try:                                   # one cheap call first, so a bad key fails before hundreds of calls
            call_model("Reply with the word ok.", models[0], key)
        except RuntimeError as e:
            raise SystemExit(f"Key check failed: {e}")
    con = db.connect(db_path)
    recs, codes = calibration_records(con)
    report = {"prompt_version": PROMPT_VERSION, "records_screened": len(recs),
              "note": "calibration records excluded by the publication-type rule are not screened", "models": {}}
    for model in models:
        with ThreadPoolExecutor(workers) as ex:
            rows = list(ex.map(lambda r: screen_one(r, model, key, call), recs))
        with open(CAL / f"screener_{model}.jsonl", "w", encoding="utf-8") as f:
            for r in sorted(rows, key=lambda r: r["record_id"]):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        rep = score(rows, codes)
        rep["tokens"] = {k: sum((r.get("usage") or {}).get(k, 0) for r in rows)
                         for k in ("input_tokens", "output_tokens")}
        report["models"][model] = rep
    (CAL / "screener_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["calibrate"])
    ap.add_argument("--models", default="claude-sonnet-5-5,claude-haiku-5-5")
    args = ap.parse_args(argv)
    rep = calibrate([m.strip() for m in args.models.split(",") if m.strip()])
    print(json.dumps({m: {k: v for k, v in r.items() if k != "missed_human_includes"}
                      for m, r in rep["models"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
