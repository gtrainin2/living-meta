import json

from pipeline import screen


def test_prompt_fills_placeholders():
    p = screen.build_prompt({"record_id": 7, "title": "T", "abstract": None, "venue": None, "year": 2025})
    assert "{{" not in p and "E10 Publication type not eligible" in p and "[no abstract in the index]" in p


def test_screen_one_retry_and_quote_check():
    rec = {"record_id": 1, "title": "ChatGPT tutor in algebra", "abstract": "A quasi-experiment with a control group.",
           "venue": "J", "year": 2025}
    replies = iter(["not json", json.dumps({"decision": "include", "exclusion_code": None,
                                            "quote": "a quasi experiment with a control group"})])
    row = screen.screen_one(rec, "m", "k", call=lambda p, m, k: (next(replies), {"input_tokens": 10}))
    assert row["decision"] == "include" and row["quote_verified"] and row["error"] is None


def test_score():
    codes = {i: {"decision": d, "exclusion_code": c} for i, (d, c) in
             enumerate([("include", ""), ("include", ""), ("exclude", "E2"), ("exclude", "E5")], 1)}
    rows = [{"record_id": 1, "decision": "unsure"}, {"record_id": 2, "decision": "exclude", "exclusion_code": "E3"},
            {"record_id": 3, "decision": "exclude", "exclusion_code": "E2"}, {"record_id": 4, "decision": "include"}]
    s = screen.score(rows, codes)
    assert s["sensitivity"] == 0.5 and s["specificity"] == 0.5 and s["exclusion_code_agreement"] == 1.0
    assert s["missed_human_includes"][0]["record_id"] == 2
