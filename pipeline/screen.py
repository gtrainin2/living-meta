"""Stage 2, title and abstract screening with the screener prompt.

    python -m pipeline.screen calibrate [--models claude-haiku-5-5]

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
PROMPT_VERSION = "screener-0.4"
API = "https://api.anthropic.com/v1/messages"


CODES = ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "E10"]
LEVEL = {"type": "string", "enum": ["met", "not_met", "unclear"]}
TOOL = {
    "name": "record_decision",
    "description": "Record the screening decision for this record.",
    "input_schema": {
        "type": "object",
        "properties": {
            "record_id": {"type": "string"},
            "stage": {"type": "string", "enum": ["abstract", "fulltext"]},
            "decision": {"type": "string", "enum": ["include", "exclude", "unsure"]},
            "exclusion_code": {"type": ["string", "null"], "enum": CODES + [None]},
            "criteria": {"type": "object", "properties": {k: LEVEL for k in
                         ("intervention", "comparison", "design", "population", "domain", "outcome")}},
            "quote": {"type": ["string", "null"]},
            "quote_location": {"type": ["string", "null"]},
            "rationale": {"type": "string"},
            "possible_duplicate_of": {"type": ["string", "null"]},
            "injection_suspected": {"type": "boolean"},
        },
        "required": ["decision", "exclusion_code", "criteria", "quote", "rationale", "injection_suspected"],
    },
}


ABSTRACT_CAP = 2000          # characters; some indexes store a full text in the abstract field


def cap(text):
    if not text or len(text) <= ABSTRACT_CAP:
        return text
    return text[:ABSTRACT_CAP].rsplit(" ", 1)[0] + " [truncated: the index stored a longer text]"


INJECTION = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?|rules)|"
    r"disregard (all |the )?(previous|prior|above) |"
    r"\byou are (now )?(an? )?(ai|assistant|language model|llm|chatgpt|claude|reviewer)\b|"
    r"(new|hidden|override) (system )?instructions?:|"
    r"(reviewers?|screeners?|ai systems?|language models?|llms?)[,:]? (must|should|please) (include|accept|rate|mark|recommend)|"
    r"(mark|rate|classify) this (paper|article|record|study) as (include|eligible|relevant|positive)", re.I)


def injection_passage(text):
    """The first passage that looks written to steer an automated reviewer, with context, or None."""
    m = INJECTION.search(text or "")
    if not m:
        return None
    return (text[max(0, m.start() - 80):m.end() + 80]).replace("\n", " ")


def build_prompt(rec, stage="abstract"):
    """Returns (fixed instructions, record message). The fixed part is identical for every record."""
    tmpl = (ROOT / "prompts" / "screener.md").read_text(encoding="utf-8").split("-->", 1)[1].lstrip()
    elig = (ROOT / "prompts" / "eligibility.md").read_text(encoding="utf-8")
    fixed, record = tmpl.split("=====RECORD=====")
    body = (f"Title: {rec['title']}\nVenue: {rec['venue'] or 'not given'}\nYear: {rec['year'] or 'not given'}\n"
            f"Abstract: {cap(rec['abstract']) or '[no abstract in the index]'}")
    fixed = fixed.replace("{{eligibility_table_and_exclusion_codes}}", elig).strip()
    record = (record.replace("{{stage}}", stage).replace("{{record_id}}", str(rec["record_id"]))
              .replace("{{record}}", body).strip())
    return fixed, record


def call_model(prompt, model, key, tries=6):
    import requests
    fixed, record = prompt if isinstance(prompt, tuple) else ("", prompt)
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": model, "max_tokens": 1200,
               "messages": [{"role": "user", "content": record}]}
    if fixed:
        payload["system"] = [{"type": "text", "text": fixed, "cache_control": {"type": "ephemeral"}}]
        payload["tools"] = [TOOL]
        payload["tool_choice"] = {"type": "tool", "name": "record_decision"}
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
        for b in js["content"]:
            if b.get("type") == "tool_use":
                return b["input"], js.get("usage", {})
        return "".join(b.get("text", "") for b in js["content"]), js.get("usage", {})
    raise RuntimeError(f"gave up after {tries} tries, last HTTP {r.status_code}")


def parse(reply):
    if isinstance(reply, dict):
        out = reply
    else:
        m = re.search(r"\{.*\}", reply, re.S)
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
            source = norm(f"{rec['title']} {rec['venue'] or ''} {rec['abstract'] or ''}")
            row["quote_verified"] = bool(out.get("quote")) and norm(out["quote"]) in source
            return row
        except Exception as e:  # malformed reply gets one retry, then goes to human review
            row.update(decision=None, error=f"{type(e).__name__}: {e}")
    return row


def calibration_records(con):
    ref = CAL / "abstract_reference_codes.csv"          # human codes after PI adjudication
    codes = {int(r["record_id"]): r for r in csv.DictReader(open(ref if ref.exists() else CAL / "abstract_human_codes.csv"))}
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
            call_model(("", "Reply with the word ok."), models[0], key)
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
        rep["tokens"] = {k: sum((r.get("usage") or {}).get(k) or 0 for r in rows)
                         for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                   "output_tokens")}
        report["models"][model] = rep
    (CAL / "screener_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["calibrate"])
    ap.add_argument("--models", default="claude-haiku-5-5")
    args = ap.parse_args(argv)
    rep = calibrate([m.strip() for m in args.models.split(",") if m.strip()])
    print(json.dumps({m: {k: v for k, v in r.items() if k != "missed_human_includes"}
                      for m, r in rep["models"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
