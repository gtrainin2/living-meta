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

Records are excluded without screening only on strong evidence of an ineligible publication type: a preprint-server or data-repository DOI with no published venue, a preprint-server record with no DOI, a dissertation, conference proceedings (venue name, an IEEE-style year prefix, an ERIC meeting-paper label, or a Semantic Scholar conference label on a venue never indexed as a journal), or a title that starts "Poster:" or "WIP:". They get `status = excluded`, `exclusion_code = E10`, and `decided_by = rule:publication_type`. A preprint DOI attached to a named journal or conference, and a repository copy with a publisher DOI or no DOI, are left for screening.

To start a run by hand, put one word in `run-request.txt` (`counts`, `baseline`, `weekly`, `dedup`, `rules`, `calibrate`, `submit`, or `poll`) and commit it.

## Where it differs from the protocol text

- Terms are spelled out with no wildcards (`math*` became math, mathematics, mathematical). OpenAlex and ERIC do not handle wildcards reliably.
- The weekly run re-queries the last 120 days by publication date. The protocol says records indexed in the last 14 days, and indexing dates are not freely queryable across these sources. Deduplication makes the wider window safe.
- arXiv and OSF are not searched, since the protocol now excludes preprints.
- Deduplication is exact DOI, then normalized title with first author surname. Title matches never merge two records with different DOIs. Those are linked as versions instead.

## Not yet verified

The three API clients were written from the public documentation and tested against fixture responses only. The first live `counts` run is the real test of the query syntax.

## Abstract screening

`python -m pipeline.batch submit` sends every record at `status = found` to the screener (prompt `prompts/screener.md`, model Claude Haiku 5.5) through the Message Batches API, in batches of 5,000. An hourly job runs `poll`, which applies finished batches: include becomes `abstract_include`, unsure becomes `abstract_unsure`, exclude becomes `excluded` with the screener's code. Failed requests return to `found`. Every decision is logged in `data/screening/abstract_decisions-*.jsonl`. Excludes with an unverified quote or no code, a 10% random sample of excludes, and any suspected prompt injection go to `data/screening/review_queue.csv` for a person.

Calibration against 162 human-coded records (adjudicated): sensitivity 25/25, specificity .83. See `data/calibration/screener_report.json`.

## Website

`python -m pipeline.site` builds `_site/index.html` from the committed data files: progress through each stage, a PRISMA flow, exclusion reasons, the searchable list of candidate studies with the screener's reasoning, the run log, the eligibility criteria (read from `prompts/eligibility.md`) and the screener calibration. The page layout lives in `site_src/template.html`.

`.github/workflows/site.yml` rebuilds and publishes the site to GitHub Pages after every run of the search workflow, and whenever the site code changes. One-time setup: in the repository's Settings → Pages, set Source to "GitHub Actions". The site is then at https://gtrainin2.github.io/living-meta/.

The pooled analysis section is a placeholder until full-text coding produces effect sizes.
