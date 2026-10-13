from pipeline import audit, db
from tests.test_search import _rec


def _row(rid, reason, code="E3", **kw):
    r = {"record_id": str(rid), "screener_decision": "exclude", "exclusion_code": code, "reason": reason,
         "title": "t", "rationale": "r", "human_decision": "", "human_code": "", "human_notes": ""}
    r.update(kw)
    return r


def test_merge_validates_and_overwrites():
    rows = [_row(1, "random audit sample"), _row(2, "random audit sample")]
    n, skipped = audit.merge(rows, [{"record_id": 1, "decision": "recode", "code": "e2", "notes": " n "},
                                    {"record_id": 2, "decision": "recode", "code": ""},
                                    {"record_id": 9, "decision": "confirm"}])
    assert n == 1 and skipped == ["2", "9"]
    assert (rows[0]["human_decision"], rows[0]["human_code"], rows[0]["human_notes"]) == ("recode", "E2", "n")


def test_apply_and_reverse(tmp_path):
    con = db.connect(tmp_path / "r.sqlite")
    for i in (1, 2):
        db.upsert(con, _rec(i, f"10.1/{i}", "J", "journal", f"Study number {i} of chatbots"), 1)
    con.execute("UPDATE records SET status='excluded', exclusion_code='E3', decided_by='screener'")
    rows = [_row(1, "random audit sample", human_decision="fulltext"),
            _row(2, "quote not found in record", human_decision="recode", human_code="E2")]
    assert audit.apply(con, rows) == {"to_fulltext": 1, "recoded": 1}
    assert audit.apply(con, rows) == {"to_fulltext": 0, "recoded": 0}
    st = {r["record_id"]: (r["status"], r["exclusion_code"]) for r in con.execute("SELECT * FROM records")}
    assert st == {1: ("abstract_include", None), 2: ("excluded", "E2")}
    rows[0]["human_decision"] = "confirm"
    audit.apply(con, rows)
    assert con.execute("SELECT status, exclusion_code FROM records WHERE record_id=1").fetchone()[:] == ("excluded", "E3")


def test_report_rate():
    rows = [_row(i, "random audit sample", human_decision="fulltext" if i < 2 else "confirm") for i in range(10)]
    rows.append(_row(99, "quote not found in record"))
    g = audit.report(rows)["groups"]
    assert g["random_sample"]["false_exclusion_rate"] == 0.2 and g["random_sample"]["audited"] == 10
    assert g["quote_not_found"] == {"total": 1, "audited": 0, "sent_to_fulltext": 0, "recoded": 0,
                                    "false_exclusion_rate": None, "ci95": [None, None]}
