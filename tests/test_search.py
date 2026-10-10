from datetime import date

from pipeline import db, search

CFG = {"start_date": "2023-01-01", "weekly_lookback_days": 120,
       "blocks": {"a": ["ChatGPT", "large language model"], "b": ["physics"], "c": ["students"]},
       "sources": ["openalex", "eric", "semantic_scholar"]}


def fake_get(url, params):
    if "openalex" in url:
        if params["cursor"] != "*":
            return {"meta": {"count": 2, "next_cursor": None}, "results": []}
        return {"meta": {"count": 2, "next_cursor": "c2"}, "results": [
            {"id": "https://openalex.org/W1", "title": "ChatGPT as a physics tutor: A randomized trial",
             "doi": "https://doi.org/10.1000/ABC", "publication_year": 2024, "publication_date": "2024-03-01",
             "type": "article", "abstract_inverted_index": {"Students": [0], "learned": [1], "more": [2]},
             "authorships": [{"author": {"display_name": "Ana Müller"}}],
             "primary_location": {"source": {"display_name": "Phys Ed", "type": "journal"}}},
            {"id": "https://openalex.org/W2", "title": "LLM feedback in introductory programming courses",
             "doi": None, "publication_year": 2025, "authorships": [{"author": {"display_name": "Bo Li"}}],
             "primary_location": None}]}
    if "eric" in url:
        return {"response": {"numFound": 1, "docs": [
            {"id": "EJ1", "title": "ChatGPT as a Physics Tutor: A Randomized Trial.", "author": ["Muller, Ana"],
             "description": "ERIC abstract", "publicationdateyear": 2024, "source": "Phys Ed",
             "url": "https://doi.org/10.1000/abc", "publicationtype": ["Journal Articles"]}]}}
    return {"total": 2, "token": None, "data": [
        {"paperId": "s1", "title": "LLM Feedback in Introductory Programming Courses", "year": 2025,
         "abstract": "S2 abstract", "authors": [{"name": "Bo Li"}], "externalIds": {"DOI": "10.1000/xyz"},
         "venue": "SIGCSE", "publicationTypes": ["Conference"], "publicationDate": "2025-02-01"},
        {"paperId": "s2", "title": "A wholly different study of chemistry chatbots", "year": 2024,
         "authors": [{"name": "C. Diaz"}], "externalIds": {}}]}


def test_normalizers():
    assert search.norm_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert search.norm_doi("not a doi") is None
    assert search.title_key("ChatGPT as a Physics Tutor: A Randomized Trial.", "Muller, Ana", 2024) is not None
    assert search.title_key("Short", "X", 2024) is None
    assert search.OpenAlex.abstract({"b": [1], "a": [0, 2]}) == "a b a"


def test_queries():
    _, p = search.SOURCES["openalex"].build(CFG, "2023-01-01")
    assert '(ChatGPT OR "large language model") AND (physics) AND (students)' in p["filter"]
    assert "from_publication_date:2023-01-01" in p["filter"]
    _, p = search.SOURCES["semantic_scholar"].build(CFG, "2023-01-01")
    assert p["query"] == '(ChatGPT | "large language model") + (physics) + (students)'
    _, p = search.SOURCES["eric"].build(CFG, "2023-01-01")
    assert "publicationdateyear:2023" in p["search"]


def test_dedup_and_rerun(tmp_path):
    path = tmp_path / "r.sqlite"
    s = search.run("baseline", CFG, path, get=fake_get, today=date(2026, 10, 9))
    # 5 fetched across sources, 3 distinct studies: DOI match (ERIC) and title match (S2).
    assert [s[k]["fetched"] for k in CFG["sources"]] == [2, 1, 2]
    assert s["total_records"] == 3 and s["new_this_run"] == 3
    assert s["eric"]["duplicate"] == 1 and s["semantic_scholar"]["duplicate"] == 1
    con = db.connect(path)
    row = con.execute("SELECT * FROM records WHERE title LIKE 'LLM feedback%'").fetchone()
    assert row["doi"] == "10.1000/xyz" and row["abstract"] == "S2 abstract"   # gaps filled from S2
    assert "openalex" in row["sources"] and "semantic_scholar" in row["sources"]
    # A later stage advances a record; a rerun must not reset it or add rows.
    con.execute("UPDATE records SET status='screened' WHERE record_id=?", (row["record_id"],))
    con.commit()
    s2 = search.run("weekly", CFG, path, get=fake_get, today=date(2026, 10, 9))
    assert s2["total_records"] == 3 and s2["new_this_run"] == 0
    assert con.execute("SELECT status FROM records WHERE record_id=?", (row["record_id"],)).fetchone()[0] == "screened"


def test_source_failure_is_isolated(tmp_path):
    def flaky(url, params):
        if "eric" in url:
            raise RuntimeError("down")
        return fake_get(url, params)
    s = search.run("baseline", CFG, tmp_path / "r.sqlite", get=flaky)
    assert s["eric"]["error"] and s["total_records"] == 3
