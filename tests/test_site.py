import re

from pipeline import site


def test_eligibility_parse(tmp_path):
    p = tmp_path / "e.md"
    p.write_text("ELIGIBILITY CRITERIA (protocol, amended 2026-10-12). Header.\n\nDesign\n  Include: RCTs.\n"
                 "  Exclude: Surveys.\n\nEXCLUSION CODES\n  E1 x\n")
    rows, amended = site.parse_eligibility(p)
    assert rows == [{"name": "Design", "include": "RCTs.", "exclude": "Surveys."}] and amended == "2026-10-12"


def test_first_author():
    assert site.first_author('["Ana Diaz", "B Lee", "C Wu"]') == "Diaz et al."
    assert site.first_author('["Ana Diaz", "B Lee"]') == "Diaz & Lee"
    assert site.first_author("[]") == ""


def test_render_fills_every_placeholder():
    page = site.render(site.build_model())
    assert not re.search(r"\{\{\w+\}\}", page)
    assert "<title>" in page[:8000]
