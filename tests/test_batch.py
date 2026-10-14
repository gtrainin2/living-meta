import json
import random

from pipeline import batch, db, screen
from tests.test_search import _rec


def test_request_shape():
    r = batch.request_for({"record_id": 12, "title": "T", "abstract": "A", "venue": None, "year": 2025})
    assert r["custom_id"] == "r12" and r["params"]["tool_choice"]["name"] == "record_decision"
    assert r["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_apply_results(tmp_path):
    con = db.connect(tmp_path / "r.sqlite")
    for i, t in enumerate(["A chatbot study in algebra classes", "A survey of nursing students and AI",
                           "Third study of LLM tutors in physics", "Fourth record that will error out"], 1):
        db.upsert(con, _rec(i, f"10.1/{i}", "J", "journal", t), 1)
    con.execute("UPDATE records SET status='screening', abstract='nursing students were surveyed'")
    def ok(i, d, code=None, quote="nursing students were surveyed"):
        return json.dumps({"custom_id": f"r{i}", "result": {"type": "succeeded", "message": {"content": [
            {"type": "tool_use", "input": {"decision": d, "exclusion_code": code, "quote": quote,
                                           "rationale": "r", "injection_suspected": False}}]}}})
    lines = [ok(1, "include"), ok(2, "exclude", "E2"), ok(3, "unsure"),
             json.dumps({"custom_id": "r4", "result": {"type": "errored", "error": {}}})]
    out, log, review = batch.apply_results(con, lines, {"id": "b1", "model": "m", "prompt_version": "p"},
                                           random.Random(0))
    assert out == {"include": 1, "unsure": 1, "exclude": 1, "failed": 1, "to_review": out["to_review"]}
    st = {r["record_id"]: (r["status"], r["exclusion_code"]) for r in con.execute("SELECT * FROM records")}
    assert st == {1: ("abstract_include", None), 2: ("excluded", "E2"), 3: ("abstract_unsure", None), 4: ("found", None)}
    assert len(log) == 3


def test_second_screen_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "OUT", tmp_path)
    batch.write_review([{"record_id": "1", "reason": "quote not found in record", "second_screen": "submitted"},
                        {"record_id": "2", "reason": "quote not found in record", "second_screen": "submitted"}])
    def line(i, d):
        return json.dumps({"custom_id": f"r{i}", "result": {"type": "succeeded", "message": {"content": [
            {"type": "tool_use", "input": {"decision": d, "exclusion_code": "E2" if d == "exclude" else None,
                                           "rationale": "r", "quote": "q", "injection_suspected": False}}]}}})
    out = batch.apply_second([line(1, "exclude"), line(2, "unsure")], {"id": "b", "model": "m", "prompt_version": "p"})
    assert out == {"agree_exclude": 1, "disagree": 1, "failed": 0}
    rows = {r["record_id"]: r for r in batch.read_review()}
    assert rows["1"]["second_screen"] == "agree_exclude" and rows["2"]["second_screen"] == "disagree"
