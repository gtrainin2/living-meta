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

- `data/records.sqlite`: one row per candidate study. `status` starts at `found`. Later stages advance it.
- `data/records.csv`: the same table as text, so weekly changes are visible in the git history.
- `runs` table and `data/last_run.json`: fetched, new, and duplicate counts per source, plus any source error.

## Where it differs from the protocol text

- Terms are spelled out with no wildcards (`math*` became math, mathematics, mathematical). OpenAlex and ERIC do not handle wildcards reliably.
- The weekly run re-queries the last 120 days by publication date. The protocol says records indexed in the last 14 days, and indexing dates are not freely queryable across these sources. Deduplication makes the wider window safe.
- arXiv and OSF are not searched, since the protocol now excludes preprints.
- Deduplication is exact DOI, then normalized title with first author surname. Title matches never merge two records with different DOIs.

## Not yet verified

The three API clients were written from the public documentation and tested against fixture responses only. The first live `counts` run is the real test of the query syntax.
