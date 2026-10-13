import json

from pipeline import screen


def test_prompt_fills_placeholders():
    fixed, rec = screen.build_prompt({"record_id": 7, "title": "T", "abstract": None, "venue": None, "year": 2025})
    assert "{{" not in fixed + rec and "E10 Publication type not eligible" in fixed
    assert "[no abstract in the index]" in rec and "Title: T" not in fixed
    fixed2, _ = screen.build_prompt({"record_id": 8, "title": "U", "abstract": "x", "venue": "V", "year": 2024})
    assert fixed == fixed2                       # identical fixed part, so it can be cached


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


def test_tool_reply_is_used_directly():
    rec = {"record_id": 2, "title": "A", "abstract": "B with \"quoted\" words", "venue": None, "year": 2025}
    reply = {"decision": "exclude", "exclusion_code": "E2", "quote": 'B with "quoted" words', "rationale": "r"}
    row = screen.screen_one(rec, "m", "k", call=lambda p, m, k: (reply, {}))
    assert row["decision"] == "exclude" and row["quote_verified"]
