"""Draw the abstract calibration sample and build the hand-coding workbook.

    python -m pipeline.calibration draw     # writes data/calibration/abstract_sample.json and the workbook

Half the sample is a simple random draw from records awaiting screening. The other half is drawn
from records whose title or abstract uses experimental-design language, because eligible studies
are rare and a purely random 200 would hold too few of them to estimate screener sensitivity.
The stratum is stored in the sample file and left out of the workbook so the coder stays blind to it.
"""
import json
import random
import re
import sys
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "calibration"
SEED = 20261009
N_RANDOM = 100
N_ENRICHED = 100
DESIGN = re.compile(r"randomi[sz]ed|\brct\b|control group|comparison group|treatment group|experimental group|"
                    r"quasi-?experiment|experimental (design|study)|pre-?test|post-?test|effect size", re.I)
CODES = [("E1", "Intervention is not learner-facing generative AI"), ("E2", "Content is not STEM"),
         ("E3", "No control group without generative AI"),
         ("E4", "Participants are not K-12 or higher education students"),
         ("E5", "No quantitative learning outcome"), ("E6", "Not an empirical study"),
         ("E9", "Full text not in English"), ("E10", "Publication type not eligible")]


def draw(db_path=ROOT / "data" / "records.sqlite"):
    con = db.connect(db_path)
    rows = con.execute("SELECT record_id, title, abstract, authors, year, venue, doi, url FROM records "
                       "WHERE status='found' ORDER BY record_id").fetchall()
    rng = random.Random(SEED)
    enriched_pool = [r for r in rows if DESIGN.search(f"{r['title']} {r['abstract'] or ''}")]
    enriched = rng.sample(enriched_pool, N_ENRICHED)
    taken = {r["record_id"] for r in enriched}
    simple = rng.sample([r for r in rows if r["record_id"] not in taken], N_RANDOM)
    sample = [(r, "enriched") for r in enriched] + [(r, "random") for r in simple]
    rng.shuffle(sample)
    meta = {"seed": SEED, "drawn_from": len(rows), "enriched_pool": len(enriched_pool),
            "random_pool": len(rows) - N_ENRICHED,
            "note": "random stratum excludes the 100 enriched draws; weight strata by pool size for population estimates",
            "items": [{"item": i + 1, "record_id": r["record_id"], "doi": r["doi"], "title": r["title"], "stratum": s}
                      for i, (r, s) in enumerate(sample)]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "abstract_sample.json").write_text(json.dumps(meta, indent=1))
    build_workbook([r for r, _ in sample], OUT / "abstract_calibration.xlsx")
    return meta


def build_workbook(records, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    font, bold = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    fill_in = PatternFill("solid", fgColor="FFF2CC")
    head = PatternFill("solid", fgColor="D9D9D9")
    wrap = Alignment(wrap_text=True, vertical="top")
    wb = Workbook()

    info = wb.active
    info.title = "Instructions"
    lines = [
        ("Abstract screening calibration set", True),
        ("200 records drawn from those awaiting screening. Code each one from the title and abstract only.", False),
        ("", False),
        ("What to fill in", True),
        ("Fill only the three yellow columns on the Coding sheet: Decision, Exclusion code, Notes.", False),
        ("Decision: include, exclude, or unsure. Pick from the dropdown.", False),
        ("Exclude only when the abstract gives positive evidence that a criterion fails. Missing information is "
         "not evidence. Use include when all criteria appear met and unsure when you cannot tell.", False),
        ("Exclusion code: required for an exclude, blank otherwise. Give the first code that applies, in order.", False),
        ("Records with no abstract are part of the set. Code them from the title.", False),
        ("", False),
        ("Example of a coded row", True),
        ("Title: ChatGPT in the EFL writing classroom: students' perceptions.   Decision: exclude.   "
         "Exclusion code: E2.   Notes: language learning, survey only.", False),
        ("", False),
        ("Exclusion codes", True),
    ] + [(f"{c}   {d}", False) for c, d in CODES] + [
        ("E7 (insufficient data) and E8 (duplicate) are not used at the abstract stage.", False),
        ("", False),
        ("About the sample", True),
        ("Half is a simple random draw. Half is drawn from records that use experimental-design language, so the "
         "set holds enough eligible studies to estimate sensitivity. The rows are shuffled and the workbook does "
         "not show which half a row came from. Source: gtrainin2/living-meta, search config 0.4.", False),
        ("", False),
        ("Progress", True),
    ]
    for i, (text, is_head) in enumerate(lines, start=1):
        c = info.cell(row=i, column=1, value=text)
        c.font, c.alignment = (bold if is_head else font), Alignment(wrap_text=True, vertical="top")
    n = len(records)
    r = len(lines) + 1
    info.cell(row=r, column=1, value="Rows coded").font = font
    info.cell(row=r, column=2, value=f"=COUNTA(Coding!$G$2:$G${n + 1})").font = font
    info.cell(row=r + 1, column=1, value="Rows remaining").font = font
    info.cell(row=r + 1, column=2, value=f"={n}-B{r}").font = font
    info.cell(row=r + 2, column=1, value="Excludes missing a code").font = font
    info.cell(row=r + 2, column=2,
              value=f'=COUNTIFS(Coding!$G$2:$G${n + 1},"exclude",Coding!$H$2:$H${n + 1},"")').font = font
    info.column_dimensions["A"].width = 110
    info.column_dimensions["B"].width = 10

    ws = wb.create_sheet("Coding")
    heads = ["Item", "Record ID", "Title", "Abstract", "Year", "Venue", "Decision", "Exclusion code", "Notes", "Link"]
    widths = [6, 9, 45, 95, 6, 28, 12, 13, 35, 30]
    for j, (h, w) in enumerate(zip(heads, widths), start=1):
        c = ws.cell(row=1, column=j, value=h)
        c.font, c.fill, c.alignment = bold, head, wrap
        ws.column_dimensions[c.column_letter].width = w
    for i, rec in enumerate(records, start=2):
        link = f"https://doi.org/{rec['doi']}" if rec["doi"] else rec["url"]
        vals = [i - 1, rec["record_id"], rec["title"], rec["abstract"] or "[no abstract in the index]",
                rec["year"], rec["venue"], None, None, None, link]
        for j, v in enumerate(vals, start=1):
            c = ws.cell(row=i, column=j, value=v)
            c.font, c.alignment = font, wrap
            if j in (7, 8, 9):
                c.fill = fill_in
        ws.cell(row=i, column=10).hyperlink = link
    dv = DataValidation(type="list", formula1='"include,exclude,unsure"', allow_blank=True)
    dc = DataValidation(type="list", formula1='"' + ",".join(c for c, _ in CODES) + '"', allow_blank=True)
    ws.add_data_validation(dv)
    ws.add_data_validation(dc)
    dv.add(f"G2:G{n + 1}")
    dc.add(f"H2:H{n + 1}")
    ws.freeze_panes = "C2"
    wb.save(path)


if __name__ == "__main__":
    if sys.argv[1:] == ["draw"]:
        m = draw()
        print(json.dumps({k: v for k, v in m.items() if k != "items"}, indent=2))
