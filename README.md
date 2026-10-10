# Living meta-analysis: generative AI and STEM learning

Stage 1 of the pipeline, the search. Plain Python, no agent.

## Run it

    pip install -r requirements.txt
    python -m pytest -q                    # offline tests
    python -m pipeline.search counts       # hit counts per source, nothing downloaded
    python -m pipeline.search baseline     # everything since the protocol start date
    python -m pipeline.search weekly       # recent window, deduplicated

Optional environment variables: `CONTACT_EMAIL`, `OPENALEX_API_KEY`, `S2_API_KEY`.

## What it writes

- `data/records-0000.jsonl` and following: the records, one per line, 5,000 per file. This is the copy kept in git. `status` starts at `found` and later stages advance it.
- `data/records.sqlite`: the working database, rebuilt from the text files when missing. Not kept in git.
- `data/records.csv`: the same table without abstracts, for opening in a spreadsheet.
- `data/runs.jsonl` and `data/last_run.json`: fetched, new, and duplicate counts per source, plus any source error.

## Versions of the same study

Records with the same title and first author but different DOIs are treated as versions of one study. A journal article outranks a conference paper, which outranks a preprint or repository copy. When one record clearly outranks the rest it is kept, and the others get `status = superseded` and a `linked_to` pointing at it. Nothing is deleted. Groups with no clear winner (two journal versions, for example) keep `status = found` and get a `link_note` asking for review.

## Exclusions made by rule

Records are excluded without screening only on strong evidence of an ineligible publication type: a preprint-server or data-repository DOI with no published venue, a preprint-server record with no DOI, or a dissertation. They get `status = excluded`, `exclusion_code = E10`, and `decided_by = rule:publication_type`. A preprint DOI attached to a named journal or conference, and a repository copy with a publisher DOI or no DOI, are left for screening.

To start a run by hand, put one word in `run-request.txt` (`counts`, `baseline`, `weekly`, `dedup`, or `rules`) and commit it.

## Where it differs from the protocol text

- Terms are spelled out with no wildcards (`math*` became math, mathematics, mathematical). OpenAlex and ERIC do not handle wildcards reliably.
- The weekly run re-queries the last 120 days by publication date. The protocol says records indexed in the last 14 days, and indexing dates are not freely queryable across these sources. Deduplication makes the wider window safe.
- arXiv and OSF are not searched, since the protocol now excludes preprints.
- Deduplication is exact DOI, then normalized title with first author surname. Title matches never merge two records with different DOIs. Those are linked as versions instead.

## Not yet verified

The three API clients were written from the public documentation and tested against fixture responses only. The first live `counts` run is the real test of the query syntax.
