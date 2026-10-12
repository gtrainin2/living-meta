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


def test_surname_formats():
    assert search.surname("Muller, Ana") == search.surname("Ana Müller") == "muller"
    assert search.surname("van der Berg, Jan") == search.surname("Jan van der Berg") == "berg"
    t = "ChatGPT as a Physics Tutor: A Randomized Trial"
    assert search.title_key(t, "Muller, Ana", 2024) == search.title_key(t + ".", "Ana Müller", 2024)


def test_dedup_merges_old_keys(tmp_path):
    path = tmp_path / "r.sqlite"
    con = db.connect(path)
    base = dict(abstract=None, year=2024, pub_date=None, venue=None, pub_type=None, url=None)
    t = "ChatGPT as a physics tutor: a randomized trial"
    a = {**base, "source": "openalex", "source_id": "W1", "title": t, "authors": ["Ana Müller"],
         "doi": "10.1/a", "title_key": "old-key-1"}
    b = {**base, "source": "eric", "source_id": "EJ1", "title": t + ".", "authors": ["Muller, Ana"],
         "doi": None, "abstract": "from eric", "title_key": "old-key-2"}
    c = {**base, "source": "eric", "source_id": "EJ2", "title": t, "authors": ["Muller, Ana"],
         "doi": "10.1/different", "title_key": "old-key-3"}
    for r in (a, b, c):
        assert db.upsert(con, r, 1) == "new"
    con.commit()
    out = search.dedup(path)
    assert out["merged"] == 1 and out["records_after"] == 2 and out["different_doi_groups"] == 1
    assert out["versions"] == {"superseded": 0, "flagged_for_review": 2}      # no type information, so review
    row = db.connect(path).execute("SELECT * FROM records WHERE doi='10.1/a'").fetchone()
    assert row["abstract"] == "from eric" and "eric" in row["sources"] and "openalex" in row["sources"]


def test_version_rank():
    assert search.version_rank("10.48550/arxiv.2511.05476", "Journal of Systems and Software", "JournalArticle") == 1
    assert search.version_rank("10.5281/zenodo.1", "Zenodo (CERN)", "repository") == 1
    assert search.version_rank("10.1145/3736181.3747157", "Conference on Global Computing Education", "Book; JournalArticle") == 2
    assert search.version_rank("10.1/x", "SIGCSE", "JournalArticle; Conference") == 2
    assert search.version_rank("10.3389/frai.2025.1", "Frontiers in Artificial Intelligence", "journal") == 3
    assert search.version_rank(None, None, None) == 0


def _rec(i, doi, venue, pub_type, title="ChatGPT as a physics tutor: a randomized trial"):
    return {"source": "s", "source_id": str(i), "title": title, "authors": ["Ana Muller"], "doi": doi,
            "abstract": None, "year": 2025, "pub_date": None, "venue": venue, "pub_type": pub_type, "url": None,
            "title_key": search.title_key(title, "Ana Muller", 2025)}


def test_link_versions_and_text_roundtrip(tmp_path):
    path = tmp_path / "r.sqlite"
    con = db.connect(path)
    db.upsert(con, _rec(1, "10.48550/arxiv.1", "arXiv.org", "repository"), 1)
    db.upsert(con, _rec(2, "10.1145/1", "SIGCSE Technical Symposium", "Conference"), 1)
    db.upsert(con, _rec(3, "10.1016/1", "Computers and Education", "journal"), 1)
    t2 = "A second study reported in two different journals"
    db.upsert(con, _rec(4, "10.2/a", "Journal A", "journal", t2), 1)
    db.upsert(con, _rec(5, "10.2/b", "Journal B", "journal", t2), 1)
    assert search.link_versions(con) == {"superseded": 2, "flagged_for_review": 2}
    rows = {r["doi"]: r for r in con.execute("SELECT * FROM records")}
    keep = rows["10.1016/1"]
    assert keep["status"] == "found" and keep["linked_to"] is None
    assert rows["10.48550/arxiv.1"]["status"] == rows["10.1145/1"]["status"] == "superseded"
    assert rows["10.1145/1"]["linked_to"] == keep["record_id"]
    assert rows["10.2/a"]["status"] == "found" and "needs review" in rows["10.2/a"]["link_note"]
    assert search.link_versions(con) == {"superseded": 0, "flagged_for_review": 0}      # safe to rerun
    # The text files alone must rebuild the same database.
    db.save(con, path)
    con.close()
    path.unlink()
    con2 = db.connect(path)
    assert con2.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 5
    assert con2.execute("SELECT status FROM records WHERE doi='10.1145/1'").fetchone()[0] == "superseded"


def test_publication_type_rule():
    rule = search.publication_type_rule
    assert rule("10.48550/arxiv.2412.03205", "arXiv.org", "JournalArticle")
    assert rule("10.5281/zenodo.1", "Zenodo (CERN European Organization for Nuclear Research)", "repository")
    assert rule("10.5281/zenodo.2", "Neliti", "repository")
    assert rule(None, "arXiv.org", "JournalArticle")
    assert rule(None, "ProQuest LLC", "Dissertations/Theses - Doctoral Dissertations")
    # A preprint DOI on a named workshop is not a preprint, but it is proceedings.
    assert rule("10.48550/arxiv.2506.09367", "Workshop on Innovative Use of NLP", "JournalArticle") == "conference proceedings"
    assert rule("10.1145/3795867.3830978", "arXiv.org", "JournalArticle") is None
    # Proceedings and posters (amendment 2026-10-12).
    assert rule("10.1145/1", "Technical Symposium on Computer Science Education", "Book; JournalArticle") == "conference proceedings"
    assert rule(None, "2025 World Engineering Education Forum - Global Engineering Deans Council", None) == "conference proceedings"
    assert rule("10.3102/2198063", "AERA Online Paper Repository", "Speeches/Meeting Papers; Reports - Research") == "conference proceedings"
    assert rule("10.18653/v1/x", "Annual Meeting of the Association for Computational Linguistics", "JournalArticle; Conference") == "conference proceedings"
    assert rule("10.1088/1742-6596/1", "Journal of Physics, Conference Series", "JournalArticle; Conference") == "conference proceedings"
    assert rule("10.1/x", "Journal of The Colloquium for Information Systems Security Education", "journal") is None
    assert rule("10.1016/x", "Informatics", "JournalArticle; Conference", None, {"informatics"}) is None
    assert rule("10.1109/x", "Information Technology Trends", "Conference", None, {"informatics"}) == "conference proceedings"
    assert rule("10.1/x", "New Vistas", "journal", "Poster: Adaptive AI tutors in extended reality") == "poster or work in progress"
    assert rule("10.1/x", "New Vistas", "journal", "Posters and learning: a study") is None
    assert rule("10.2478/rem-2025-0012", "DOAJ (DOAJ: Directory of Open Access Journals)", "repository") is None
    assert rule(None, "UKnowledge (University of Kentucky)", "repository") is None
    assert rule("10.1108/dlp-05-2025-206", "Digital Library Perspectives", "journal") is None
    assert rule("10.24042/biosfer.v14i2.17111", "Biosfer Jurnal Tadris Biologi", "journal") is None


def test_apply_rules_only_touches_found(tmp_path):
    con = db.connect(tmp_path / "r.sqlite")
    db.upsert(con, _rec(1, "10.48550/arxiv.1", "arXiv.org", "repository", "A preprint only study of chatbots in physics"), 1)
    db.upsert(con, _rec(2, "10.48550/arxiv.2", "arXiv.org", "repository", "A second preprint that was already screened"), 1)
    db.upsert(con, _rec(3, "10.1016/1", "Computers and Education", "journal", "A journal article about chatbots in algebra"), 1)
    con.execute("UPDATE records SET status='screened' WHERE doi='10.48550/arxiv.2'")
    out = search.apply_rules(con)
    assert out["remaining_to_screen"] == 1 and sum(v for k, v in out.items() if k != "remaining_to_screen") == 1
    got = {r["doi"]: (r["status"], r["exclusion_code"]) for r in con.execute("SELECT * FROM records")}
    assert got == {"10.48550/arxiv.1": ("excluded", "E10"), "10.48550/arxiv.2": ("screened", None),
                   "10.1016/1": ("found", None)}
