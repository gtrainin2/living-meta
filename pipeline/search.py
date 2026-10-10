"""Stage 1, search. Plain code, no agent.

    python -m pipeline.search counts              # hit counts per source, no download
    python -m pipeline.search baseline            # everything since start_date
    python -m pipeline.search weekly              # recent window, deduplicated against the database

Environment: CONTACT_EMAIL (sent to OpenAlex), OPENALEX_API_KEY and S2_API_KEY (optional).
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import yaml

from . import db

ROOT = Path(__file__).resolve().parent.parent


# ---------- normalization ----------

def norm_doi(doi):
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi)
    return doi if doi.startswith("10.") else None


def title_key(title, first_author, year):
    """Fallback identity when a DOI is missing: normalized title + first author surname."""
    if not title:
        return None
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    if len(t) < 15:                      # too short to be a safe key
        return None
    return f"{t}|{surname(first_author)}"


def surname(name):
    """Surname from 'Ana Muller' or from ERIC's 'Muller, Ana'. Last word of the surname part."""
    a = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    a = a.split(",")[0] if "," in a else a
    a = re.sub(r"[^a-z ]", " ", a).split()
    return a[-1] if a else ""


def record(source, source_id, title, abstract, authors, year, pub_date, venue, pub_type, doi, url):
    authors = [a for a in (authors or []) if a]
    return {
        "source": source, "source_id": str(source_id), "title": (title or "").strip(),
        "abstract": (abstract or "").strip() or None, "authors": authors,
        "year": int(year) if year else None, "pub_date": pub_date, "venue": venue or None,
        "pub_type": pub_type or None, "doi": norm_doi(doi), "url": url or None,
        "title_key": title_key(title, authors[0] if authors else "", year),
    }


def q(term):
    return f'"{term}"' if re.search(r"[\s\-.]", term) else term


# ---------- sources ----------
# Each source exposes build(cfg, since) -> (url, params), count(json) -> int,
# and pages(get, cfg, since) -> iterator of normalized records.

class OpenAlex:
    name = "openalex"
    url = "https://api.openalex.org/works"

    def build(self, cfg, since):
        blocks = " AND ".join("(" + " OR ".join(q(t) for t in terms) + ")" for terms in cfg["blocks"].values())
        params = {"filter": f"title_and_abstract.search:{blocks},from_publication_date:{since},type:article",
                  "per-page": 200}
        if os.environ.get("CONTACT_EMAIL"):
            params["mailto"] = os.environ["CONTACT_EMAIL"]
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        return self.url, params

    def count(self, js):
        return js["meta"]["count"]

    @staticmethod
    def abstract(inv):
        if not inv:
            return None
        pos = sorted((p, w) for w, ps in inv.items() for p in ps)
        return " ".join(w for _, w in pos)

    def pages(self, get, cfg, since):
        url, params = self.build(cfg, since)
        cursor = "*"
        while cursor:
            js = get(url, {**params, "cursor": cursor})
            for w in js.get("results", []):
                src = ((w.get("primary_location") or {}).get("source") or {})
                yield record(self.name, w["id"], w.get("title"), self.abstract(w.get("abstract_inverted_index")),
                             [(a.get("author") or {}).get("display_name") for a in w.get("authorships", [])],
                             w.get("publication_year"), w.get("publication_date"), src.get("display_name"),
                             src.get("type") or w.get("type"), w.get("doi"), w.get("id"))
            cursor = js.get("meta", {}).get("next_cursor") if js.get("results") else None


class Eric:
    name = "eric"
    url = "https://api.ies.ed.gov/eric/"
    fields = "id,title,author,description,publicationdateyear,source,url,peerreviewed,publicationtype"

    def build(self, cfg, since):
        blocks = " AND ".join("(" + " OR ".join(q(t) for t in terms) + ")" for terms in cfg["blocks"].values())
        years = " OR ".join(f"publicationdateyear:{y}" for y in range(int(since[:4]), date.today().year + 1))
        return self.url, {"search": f"{blocks} AND ({years})", "format": "json", "rows": 200, "fields": self.fields}

    def count(self, js):
        return js["response"]["numFound"]

    def pages(self, get, cfg, since):
        url, params = self.build(cfg, since)
        start = 0
        while True:
            js = get(url, {**params, "start": start})
            docs = js["response"]["docs"]
            for d in docs:
                link = d.get("url") or ""
                yield record(self.name, d["id"], d.get("title"), d.get("description"), d.get("author"),
                             d.get("publicationdateyear"), None, d.get("source"),
                             "; ".join(d.get("publicationtype") or []), link if "doi.org" in link else None,
                             f"https://eric.ed.gov/?id={d['id']}")
            start += len(docs)
            if not docs or start >= js["response"]["numFound"]:
                break


class SemanticScholar:
    name = "semantic_scholar"
    url = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
    fields = "title,abstract,year,venue,externalIds,publicationTypes,publicationDate,authors"

    def build(self, cfg, since):
        blocks = " + ".join("(" + " | ".join(q(t) for t in terms) + ")" for terms in cfg["blocks"].values())
        return self.url, {"query": blocks, "fields": self.fields, "publicationDateOrYear": f"{since}:"}

    def count(self, js):
        return js["total"]

    def pages(self, get, cfg, since):
        url, params = self.build(cfg, since)
        token = None
        while True:
            js = get(url, {**params, **({"token": token} if token else {})})
            for p in js.get("data", []):
                ext = p.get("externalIds") or {}
                yield record(self.name, p["paperId"], p.get("title"), p.get("abstract"),
                             [a.get("name") for a in p.get("authors") or []], p.get("year"),
                             p.get("publicationDate"), p.get("venue"),
                             "; ".join(p.get("publicationTypes") or []), ext.get("DOI"),
                             f"https://www.semanticscholar.org/paper/{p['paperId']}")
            token = js.get("token")
            if not token or not js.get("data"):
                break


SOURCES = {s.name: s for s in (OpenAlex(), Eric(), SemanticScholar())}


# ---------- http ----------

def http_get(url, params, tries=5):
    import requests
    headers = {"User-Agent": f"living-meta-analysis ({os.environ.get('CONTACT_EMAIL', 'no-contact')})"}
    if "semanticscholar" in url and os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    for i in range(tries):
        r = requests.get(url, params=params, headers=headers, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(60, 2 ** i * 3))
            continue
        r.raise_for_status()
        time.sleep(1.1 if "semanticscholar" in url else 0.15)
        return r.json()
    raise RuntimeError(f"{url} kept failing after {tries} tries (last status {r.status_code})")


# ---------- run ----------

def load_config(path=None):
    return yaml.safe_load(Path(path or ROOT / "config" / "search.yaml").read_text())


def run(mode, cfg, db_path, get=http_get, today=None):
    today = today or date.today()
    since = cfg["start_date"] if mode == "baseline" else str(today - timedelta(days=cfg["weekly_lookback_days"]))
    con = db.connect(db_path)
    run_id = db.start_run(con, mode, since)
    summary = {}
    for name in cfg["sources"]:
        s = {"fetched": 0, "new": 0, "duplicate": 0, "error": None}
        try:
            for rec in SOURCES[name].pages(get, cfg, since):
                if not rec["title"]:
                    continue
                s["fetched"] += 1
                s[db.upsert(con, rec, run_id)] += 1
            con.commit()
        except Exception as e:           # one source failing must not lose the others
            con.commit()
            s["error"] = f"{type(e).__name__}: {e}"
        summary[name] = s
    summary["total_records"] = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    summary["new_this_run"] = con.execute("SELECT COUNT(*) FROM records WHERE first_run=?", (run_id,)).fetchone()[0]
    summary["versions"] = link_versions(con)
    summary["rules"] = apply_rules(con)
    db.finish_run(con, run_id, summary)
    db.save(con, db_path)
    export_csv(con, Path(db_path).with_suffix(".csv"))
    return summary


def export_csv(con, path):
    """Plain-text copy of the records table so each week's changes show up in the git history."""
    import csv
    cols = ["record_id", "status", "exclusion_code", "decided_by", "linked_to", "link_note", "doi", "title", "authors", "year", "venue",
            "pub_type", "url", "sources", "first_run", "first_seen"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(con.execute(f"SELECT {','.join(cols)} FROM records ORDER BY record_id"))


def dedup(db_path):
    """Recompute every title key with the current rules, then merge records that now match.

    A merge keeps the earliest record, unions the sources, and fills gaps. It is skipped when
    the DOIs conflict or when a record to be removed has already moved past 'found'.
    """
    con = db.connect(db_path)
    before = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    for r in con.execute("SELECT record_id, title, authors, year FROM records").fetchall():
        authors = json.loads(r["authors"] or "[]")
        con.execute("UPDATE records SET title_key=? WHERE record_id=?",
                    (title_key(r["title"], authors[0] if authors else "", r["year"]), r["record_id"]))
    merged = skipped = 0
    keys = con.execute("SELECT title_key FROM records WHERE title_key IS NOT NULL "
                       "GROUP BY title_key HAVING COUNT(*) > 1").fetchall()
    for (key,) in keys:
        rows = con.execute("SELECT * FROM records WHERE title_key=? ORDER BY record_id", (key,)).fetchall()
        keep = dict(rows[0])
        for r in rows[1:]:
            if (keep["doi"] and r["doi"] and keep["doi"] != r["doi"]) or r["status"] != "found":
                skipped += 1
                continue
            sources = {**json.loads(r["sources"]), **json.loads(keep["sources"])}
            keep["sources"] = json.dumps(sources)
            for k in ("doi", "abstract", "venue", "pub_date", "url", "pub_type"):
                keep[k] = keep[k] or r[k]
            con.execute("DELETE FROM records WHERE record_id=?", (r["record_id"],))
            con.execute("UPDATE records SET sources=?, doi=?, abstract=?, venue=?, pub_date=?, url=?, pub_type=? "
                        "WHERE record_id=?", (keep["sources"], keep["doi"], keep["abstract"], keep["venue"],
                                              keep["pub_date"], keep["url"], keep["pub_type"], keep["record_id"]))
            merged += 1
    con.commit()
    versions = link_versions(con)
    db.save(con, db_path)
    export_csv(con, Path(db_path).with_suffix(".csv"))
    after = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    return {"records_before": before, "merged": merged, "different_doi_groups": skipped,
            "records_after": after, "versions": versions}


# DOI prefixes that belong to preprint servers and data repositories, not to publishers.
PREPRINT_DOI = ("10.48550/", "10.1101/", "10.64898/", "10.5281/", "10.31234/", "10.35542/", "10.31219/",
                "10.2139/", "10.21203/", "10.20944/", "10.36227/", "10.26434/", "10.32388/", "10.3929/",
                "10.25968/", "10.6084/", "10.17605/")
PREPRINT_VENUE = re.compile(r"arxiv|[a-z]rxiv\b|zenodo|\bssrn\b|preprint|research square|figshare|"
                            r"\bosf\b|open science framework|social science research network", re.I)
PROCEEDINGS = re.compile(r"conference|proceedings|symposium|workshop|meeting papers|congress", re.I)


def version_rank(doi, venue, pub_type):
    """3 journal article, 2 conference paper, 1 preprint or repository copy, 0 cannot tell."""
    doi, venue, pub_type = doi or "", venue or "", pub_type or ""
    if doi.startswith(PREPRINT_DOI) or doi.endswith("/pdf") or pub_type == "repository" or PREPRINT_VENUE.search(venue):
        return 1
    if PROCEEDINGS.search(pub_type) or PROCEEDINGS.search(venue):
        return 2
    if "ournal" in pub_type or pub_type == "article":
        return 3
    return 0


def publication_type_rule(doi, venue, pub_type):
    """Reason a record is ineligible on publication type alone, or None.

    This rule runs with no human or agent check, so it only fires on strong evidence. A preprint
    DOI attached to a named journal or conference is left for screening, because the indexes often
    attach an arXiv DOI to a paper that was later published. So is a repository copy with a
    publisher DOI or no DOI, because many of those are published articles held in a repository.
    """
    doi, venue, pub_type = doi or "", venue or "", pub_type or ""
    server_venue = bool(PREPRINT_VENUE.search(venue))
    if doi.startswith(PREPRINT_DOI) and (server_venue or not venue or pub_type == "repository"):
        return "preprint or repository deposit, no published venue"
    if server_venue and not doi:
        return "preprint server record with no DOI"
    if "Dissertations/Theses" in pub_type:
        return "dissertation or thesis"
    return None


def apply_rules(con):
    """Exclude records still at 'found' that fail the publication-type rule. Nothing is deleted."""
    out = {}
    for r in con.execute("SELECT record_id, doi, venue, pub_type FROM records WHERE status='found'").fetchall():
        reason = publication_type_rule(r["doi"], r["venue"], r["pub_type"])
        if reason:
            con.execute("UPDATE records SET status='excluded', exclusion_code='E10', decided_by=?, link_note=? "
                        "WHERE record_id=?", ("rule:publication_type", reason, r["record_id"]))
            out[reason] = out.get(reason, 0) + 1
    con.commit()
    out["remaining_to_screen"] = con.execute("SELECT COUNT(*) FROM records WHERE status='found'").fetchone()[0]
    return out


def link_versions(con):
    """Handle records with the same title and first author but different DOIs.

    When one record clearly outranks the rest (journal over conference paper over preprint), it is
    kept and the others are marked 'superseded' with a link to it. Nothing is deleted. Groups with
    no clear winner stay in play and are flagged for a person to review.
    """
    out = {"superseded": 0, "flagged_for_review": 0}
    keys = con.execute("SELECT title_key FROM records WHERE title_key IS NOT NULL AND status != 'superseded' "
                       "GROUP BY title_key HAVING COUNT(*) > 1").fetchall()
    for (key,) in keys:
        rows = con.execute("SELECT * FROM records WHERE title_key=? AND status != 'superseded' "
                           "ORDER BY record_id", (key,)).fetchall()
        ranks = [version_rank(r["doi"], r["venue"], r["pub_type"]) for r in rows]
        top = max(ranks)
        if ranks.count(top) == 1 and 0 not in ranks:
            keep = rows[ranks.index(top)]
            for r, rank in zip(rows, ranks):
                if r["record_id"] != keep["record_id"] and r["status"] == "found":
                    con.execute("UPDATE records SET status='superseded', linked_to=?, link_note=? WHERE record_id=?",
                                (keep["record_id"], "earlier version of the linked record", r["record_id"]))
                    out["superseded"] += 1
        else:
            for r in rows:
                other = rows[0]["record_id"] if r["record_id"] != rows[0]["record_id"] else rows[1]["record_id"]
                if not r["link_note"]:
                    con.execute("UPDATE records SET linked_to=?, link_note=? WHERE record_id=?",
                                (other, "possible version of the linked record, needs review", r["record_id"]))
                    out["flagged_for_review"] += 1
    con.commit()
    return out


def counts(cfg, get=http_get):
    out = {}
    for name in cfg["sources"]:
        src = SOURCES[name]
        url, params = src.build(cfg, cfg["start_date"])
        small = {**params, **{k: 1 for k in ("per-page", "rows") if k in params}}
        try:
            out[name] = src.count(get(url, small))
        except Exception as e:
            out[name] = f"error: {e}"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["counts", "baseline", "weekly", "dedup", "rules", "urls"])
    ap.add_argument("--db", default=str(ROOT / "data" / "records.sqlite"))
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.mode == "urls":              # print the count queries, for checking by hand
        from urllib.parse import urlencode
        for name in cfg["sources"]:
            url, params = SOURCES[name].build(cfg, cfg["start_date"])
            print(name, url + "?" + urlencode(params))
        return 0
    if args.mode == "dedup":
        print(json.dumps(dedup(args.db), indent=2))
        return 0
    if args.mode == "rules":
        con = db.connect(args.db)
        result = apply_rules(con)
        db.save(con, args.db)
        export_csv(con, Path(args.db).with_suffix(".csv"))
        print(json.dumps(result, indent=2))
        return 0
    result = counts(cfg) if args.mode == "counts" else run(args.mode, cfg, args.db)
    print(json.dumps(result, indent=2))
    errors = [v for v in result.values() if isinstance(v, dict) and v.get("error")]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
