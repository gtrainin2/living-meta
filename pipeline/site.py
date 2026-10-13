"""Build the public website from the committed data files.

    python -m pipeline.site            # writes _site/index.html
    python -m pipeline.site --fragment # also writes _site/fragment.html (page body only)

Reads only what the pipeline already commits: data/records-*.jsonl, data/runs.jsonl,
data/screening/*, data/calibration/screener_report.json and prompts/eligibility.md.
Standard library only, so the site build never depends on the API clients.
"""
import csv
import glob
import html
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "_site"
TEMPLATE = ROOT / "site_src" / "template.html"
CENTRAL = ZoneInfo("America/Chicago")
REPO_URL = "https://github.com/gtrainin2/living-meta"

CODE_LABELS = {
    "E1": "Not learner-facing generative AI",
    "E2": "Content is not STEM",
    "E3": "No control group without generative AI",
    "E4": "Not K-12 or higher education students",
    "E5": "No quantitative learning outcome",
    "E6": "Not an empirical study",
    "E7": "Insufficient data after author query",
    "E8": "Duplicate or superseded version",
    "E9": "Full text not in English",
    "E10": "Publication type not eligible",
}
SOURCE_LABELS = {"openalex": "OpenAlex", "eric": "ERIC", "semantic_scholar": "Semantic Scholar"}
CANDIDATE = {"abstract_include", "abstract_unsure"}


# ---------- loading ----------

def load_jsonl(pattern):
    rows = []
    for f in sorted(glob.glob(str(pattern))):
        with open(f, encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    return rows


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def parse_eligibility(path):
    """Criteria rows from prompts/eligibility.md: a name line, then indented Include:/Exclude: lines."""
    rows, cur = [], None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    amended = re.search(r"amended (\d{4}-\d{2}-\d{2})", text)
    for line in text.split("EXCLUSION CODES")[0].splitlines()[1:]:
        if not line.strip():
            continue
        m = re.match(r"\s+(Include|Exclude):\s*(.*)", line)
        if m and cur:
            cur[m.group(1).lower()] = m.group(2).strip()
        elif not line.startswith(" "):
            cur = {"name": line.strip(), "include": "", "exclude": ""}
            rows.append(cur)
    return rows, amended.group(1) if amended else None


def central(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(CENTRAL)


def first_author(authors):
    try:
        names = json.loads(authors) if isinstance(authors, str) else (authors or [])
    except json.JSONDecodeError:
        names = [authors]
    if not names:
        return ""
    last = str(names[0]).split()[-1] if str(names[0]).split() else str(names[0])
    return last + (" et al." if len(names) > 2 else f" & {str(names[1]).split()[-1]}" if len(names) == 2 else "")


# ---------- assembling the numbers ----------

def build_model():
    records = load_jsonl(DATA / "records-*.jsonl")
    runs = load_jsonl(DATA / "runs.jsonl")
    for r in runs:
        r["summary"] = json.loads(r["summary"]) if isinstance(r.get("summary"), str) else (r.get("summary") or {})
    batches = load_json(DATA / "screening" / "batches.json", [])
    decisions = {d["record_id"]: d for d in load_jsonl(DATA / "screening" / "abstract_decisions-*.jsonl")}
    calib = load_json(DATA / "calibration" / "screener_report.json", {})
    criteria, amended = parse_eligibility(ROOT / "prompts" / "eligibility.md")

    status = Counter(r["status"] for r in records)
    per_source = Counter()
    for r in records:
        try:
            srcs = json.loads(r["sources"]) if isinstance(r["sources"], str) else r["sources"]
        except (json.JSONDecodeError, TypeError):
            srcs = {}
        for s in srcs or {}:
            per_source[s] += 1

    excluded = [r for r in records if r["status"] == "excluded"]
    by_rule = sum(1 for r in excluded if (r.get("decided_by") or "").startswith("rule:"))
    by_screen = len(excluded) - by_rule
    codes = Counter(r.get("exclusion_code") or "uncoded" for r in excluded)
    screen_codes = Counter(r.get("exclusion_code") or "uncoded" for r in excluded
                           if not (r.get("decided_by") or "").startswith("rule:"))

    latest_run = max((r["run_id"] for r in runs), default=None)
    review_path = DATA / "screening" / "review_queue.csv"
    review_total = review_done = 0
    if review_path.exists():
        with open(review_path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                review_total += 1
                review_done += bool((row.get("human_decision") or "").strip())

    candidates = []
    for r in records:
        if r["status"] not in CANDIDATE:
            continue
        d = decisions.get(r["record_id"], {})
        link = f"https://doi.org/{r['doi']}" if r.get("doi") else r.get("url")
        candidates.append({
            "id": r["record_id"], "t": r.get("title") or "(untitled)", "a": first_author(r.get("authors")),
            "y": r.get("year"), "v": r.get("venue") or "", "u": link,
            "s": "include" if r["status"] == "abstract_include" else "unsure",
            "n": r.get("first_run") == latest_run and latest_run is not None and
                 any(x["run_id"] == latest_run and x["mode"] == "weekly" for x in runs),
            "why": d.get("rationale") or "",
        })
    candidates.sort(key=lambda c: (c["s"] != "include", -(c["y"] or 0), c["t"].lower()))

    stamps = [r["started"] for r in runs] + [b.get("applied") or b.get("created") for b in batches]
    updated = central(max((s for s in stamps if s), default=None))

    run_rows = []
    for r in runs:
        s = r["summary"]
        errors = [f"{SOURCE_LABELS.get(k, k)}: {v['error']}" for k, v in s.items()
                  if isinstance(v, dict) and v.get("error")]
        run_rows.append({
            "id": r["run_id"], "when": central(r["started"]), "mode": r["mode"], "since": r.get("since"),
            "fetched": {k: s[k]["fetched"] for k in SOURCE_LABELS if isinstance(s.get(k), dict)},
            "new": s.get("new_this_run", 0), "total": s.get("total_records"),
            "errors": errors,
        })

    m = (calib.get("models") or {})
    cal_model, cal = next(iter(m.items()), (None, {}))

    return {
        "records": len(records), "status": status, "per_source": per_source,
        "superseded": status.get("superseded", 0), "excluded_rule": by_rule, "excluded_screen": by_screen,
        "screened": by_screen + status.get("abstract_include", 0) + status.get("abstract_unsure", 0)
                    + status.get("found", 0) + status.get("screening", 0),
        "awaiting": status.get("found", 0) + status.get("screening", 0),
        "codes": codes, "screen_codes": screen_codes,
        "include": status.get("abstract_include", 0), "unsure": status.get("abstract_unsure", 0),
        "fulltext_done": sum(v for k, v in status.items() if k.startswith("fulltext_")),
        "analysed": status.get("included", 0),
        "review_total": review_total, "review_done": review_done,
        "candidates": candidates, "runs": run_rows, "batches": batches,
        "updated": updated, "calib": cal, "calib_model": cal_model,
        "calib_prompt": calib.get("prompt_version"), "criteria": criteria, "amended": amended,
    }


# ---------- rendering ----------

E = html.escape


def fmt(n):
    return f"{n:,}" if isinstance(n, int) else ("–" if n is None else str(n))


def prisma_svg(m):
    """PRISMA 2020-style flow, drawn with one fixed grid. Main column left, removals right."""
    W, colx, colw, sidex, sidew = 760, 20, 400, 470, 270

    def box(x, y, w, h, lines, cls="box"):
        out = [f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="3"/>']
        for i, (txt, kind) in enumerate(lines):
            out.append(f'<text class="t-{kind}" x="{x + 14}" y="{y + 24 + i * 19}">{E(txt)}</text>')
        return "".join(out)

    def down(y1, y2):
        x = colx + colw / 2
        return f'<line class="arrow" x1="{x}" y1="{y1}" x2="{x}" y2="{y2 - 2}" marker-end="url(#ah)"/>'

    def side(y):
        return (f'<line class="arrow" x1="{colx + colw}" y1="{y}" x2="{sidex - 2}" y2="{y}" '
                f'marker-end="url(#ah)"/>')

    src = " · ".join(f"{SOURCE_LABELS[k]} {fmt(m['per_source'].get(k, 0))}" for k in SOURCE_LABELS)
    top_codes = [(c, n) for c, n in sorted(m["screen_codes"].items(), key=lambda x: -x[1])][:4]
    parts = [
        '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="arrowhead"/></marker></defs>',
        '<text class="t-stage" x="20" y="18">IDENTIFICATION</text>',
        box(colx, 28, colw, 72, [(f"Unique records identified: {fmt(m['records'])}", "head"),
                                 ("Records indexed by each source (overlapping):", "sub"),
                                 (src, "sub")]),
        side(64),
        box(sidex, 28, sidew, 72, [("Removed before screening", "head"),
                                   (f"Superseded versions: {fmt(m['superseded'])}", "sub"),
                                   (f"Ineligible publication type: {fmt(m['excluded_rule'])}", "sub")], "box side"),
        down(100, 140),
        '<text class="t-stage" x="20" y="132">SCREENING</text>',
        box(colx, 140, colw, 72, [(f"Titles and abstracts screened: {fmt(m['screened'])}", "head"),
                                  (f"Awaiting screening: {fmt(m['awaiting'])}", "sub"),
                                  (f"Human audit: {fmt(m['review_done'])} of {fmt(m['review_total'])} checked", "sub")]),
        side(176),
        box(sidex, 140, sidew, 34 + 19 * (len(top_codes) + 1),
            [(f"Excluded at abstract: {fmt(m['excluded_screen'])}", "head")] +
            [(f"{c}  {CODE_LABELS.get(c, c)[:26]}: {fmt(n)}", "sub") for c, n in top_codes], "box side"),
        down(212, 262),
        '<text class="t-stage" x="20" y="254">ELIGIBILITY</text>',
        box(colx, 262, colw, 72, [(f"Candidates for full-text review: {fmt(m['include'] + m['unsure'])}", "head"),
                                  (f"Likely eligible {fmt(m['include'])} · unsure {fmt(m['unsure'])}", "sub"),
                                  (f"Full texts assessed: {fmt(m['fulltext_done'])}", "sub")]),
        down(334, 374),
        '<text class="t-stage" x="20" y="366">INCLUDED</text>',
        box(colx, 374, colw, 52, [(f"Studies in the meta-analysis: {fmt(m['analysed'])}", "head"),
                                  ("Pooling begins once full-text coding is done", "sub")], "box pending"),
    ]
    return (f'<svg class="prisma" viewBox="0 0 {W} 440" role="img" '
            f'aria-label="PRISMA flow diagram of records through each stage">{"".join(parts)}</svg>')


def codes_html(m):
    total = sum(m["codes"].values()) or 1
    peak = max(m["codes"].values(), default=1)
    order = sorted(m["codes"].items(), key=lambda x: -x[1])
    rows = []
    for code, n in order:
        rows.append(
            f'<div class="bar-row"><span class="bar-code">{E(code)}</span>'
            f'<span class="bar-label">{E(CODE_LABELS.get(code, code))}</span>'
            f'<span class="bar-track"><span class="bar" style="width:{100 * n / peak:.1f}%"></span></span>'
            f'<span class="bar-n">{fmt(n)} <small>{100 * n / total:.0f}%</small></span></div>')
    return "".join(rows)


def stages_html(m):
    audit = f"{fmt(m['review_done'])} of {fmt(m['review_total'])} audit records checked"
    stages = [
        ("Search", "done", f"{fmt(m['records'])} unique records from three databases"),
        ("Rule-based exclusions", "done", f"{fmt(m['excluded_rule'] + m['superseded'])} removed by publication type or as versions"),
        ("Abstract screening", "active" if m["awaiting"] or m["review_done"] < m["review_total"] else "done",
         f"AI screener done for {fmt(m['screened'] - m['awaiting'])} records; {audit}"),
        ("Full-text review", "active" if m["fulltext_done"] else "todo",
         f"{fmt(m['fulltext_done'])} of {fmt(m['include'] + m['unsure'])} assessed"),
        ("Data extraction", "active" if m["analysed"] else "todo", "Effect sizes and moderators coded per study"),
        ("Pooled analysis", "active" if m["analysed"] else "todo", "Random-effects model, updated each week"),
    ]
    label = {"done": "Done", "active": "In progress", "todo": "Not started"}
    return "".join(
        f'<li class="stage {s}"><span class="dot" aria-hidden="true"></span><div><div class="stage-top">'
        f'<strong>{E(n)}</strong><span class="pill {s}">{label[s]}</span></div><p>{E(d)}</p></div></li>'
        for n, s, d in stages)


def runs_html(m):
    rows = []
    for r in reversed(m["runs"]):
        when = r["when"].strftime("%b %-d, %Y") if r["when"] else "–"
        fetched = " · ".join(f"{SOURCE_LABELS[k]} {fmt(v)}" for k, v in r["fetched"].items())
        err = "".join(f'<div class="err">{E(e[:140])}</div>' for e in r["errors"])
        rows.append(f"<tr><td>{when}</td><td><span class=\"mode\">{E(r['mode'])}</span></td>"
                    f"<td>{E(r['since'] or '')}</td><td class=\"num\">{fmt(r['new'])}</td>"
                    f"<td class=\"fetched\">{fetched}{err}</td></tr>")
    return "".join(rows)


def batches_html(m):
    rows = []
    for b in reversed(m["batches"]):
        res = b.get("result") or {}
        when = central(b.get("applied") or b.get("created"))
        rows.append(f"<tr><td>{when.strftime('%b %-d, %Y') if when else '–'}</td>"
                    f"<td>{E(b.get('prompt_version') or '')}</td><td class=\"num\">{fmt(b.get('n'))}</td>"
                    f"<td class=\"num\">{fmt(res.get('include'))}</td><td class=\"num\">{fmt(res.get('unsure'))}</td>"
                    f"<td class=\"num\">{fmt(res.get('exclude'))}</td><td>{E(b.get('state') or '')}</td></tr>")
    return "".join(rows)


def criteria_html(m):
    return "".join(
        f"<tr><th scope=\"row\">{E(c['name'])}</th><td>{E(c['include'])}</td><td>{E(c['exclude'])}</td></tr>"
        for c in m["criteria"])


def calib_html(m):
    c = m["calib"]
    if not c:
        return "<p>No calibration report yet.</p>"
    t = c.get("table", {})
    sens_ci = c.get("sensitivity_95ci") or [None, None]
    spec_ci = c.get("specificity_95ci") or [None, None]
    return (
        f'<div class="cal"><div><span class="cal-n">{t.get("pass_inc", 0)}/{c.get("human_includes", 0)}</span>'
        f'<span class="cal-l">human includes kept<br>sensitivity {c.get("sensitivity", 0):.2f} '
        f'(95% CI {sens_ci[0]:.2f}–{sens_ci[1]:.2f})</span></div>'
        f'<div><span class="cal-n">{c.get("specificity", 0):.2f}</span>'
        f'<span class="cal-l">specificity<br>95% CI {spec_ci[0]:.2f}–{spec_ci[1]:.2f}</span></div>'
        f'<div><span class="cal-n">{c.get("kappa_pass_vs_exclude", 0):.2f}</span>'
        f'<span class="cal-l">Cohen\'s κ, pass vs exclude<br>{c.get("records_scored", 0)} adjudicated records</span></div></div>'
        f'<p class="note">Screener {E(m["calib_model"] or "")}, prompt {E(m["calib_prompt"] or "")}. '
        f'The screener is tuned to pass anything it cannot rule out; a person reviews every pass at full text.</p>')


def render(m):
    tpl = TEMPLATE.read_text(encoding="utf-8")
    updated = m["updated"].strftime("%B %-d, %Y") if m["updated"] else "not yet run"
    data = json.dumps(m["candidates"], ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    fill = {
        "UPDATED": E(updated),
        "RECORDS": fmt(m["records"]),
        "CANDIDATES": fmt(m["include"] + m["unsure"]),
        "INCLUDE": fmt(m["include"]),
        "UNSURE": fmt(m["unsure"]),
        "ANALYSED": fmt(m["analysed"]),
        "NEW_THIS_WEEK": fmt(sum(1 for c in m["candidates"] if c["n"])),
        "STAGES": stages_html(m),
        "PRISMA": prisma_svg(m),
        "CODES": codes_html(m),
        "RUNS": runs_html(m),
        "BATCHES": batches_html(m),
        "CRITERIA": criteria_html(m),
        "AMENDED": E(m["amended"] or ""),
        "CALIB": calib_html(m),
        "REPO": REPO_URL,
        "DATA": data,
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda x: fill[x.group(1)], tpl)


def wrap(fragment):
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '</head><body>\n' + fragment + '\n</body></html>\n')


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    m = build_model()
    page = render(m)
    OUT.mkdir(exist_ok=True)
    (OUT / "index.html").write_text(wrap(page), encoding="utf-8")
    (OUT / ".nojekyll").write_text("")
    if "--fragment" in argv:
        (OUT / "fragment.html").write_text(page, encoding="utf-8")
    print(json.dumps({"records": m["records"], "candidates": len(m["candidates"]),
                      "updated": m["updated"].isoformat() if m["updated"] else None}))


if __name__ == "__main__":
    main()
