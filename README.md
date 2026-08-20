# jobhunt

Local CLI job sourcing and application tracking. Full architecture in `PLAN.md`.

Phases 1 to 4 are built: skeleton, schema, adapter protocol, dedupe, board
discovery, six ATS adapters, and five aggregator feeds. Ranking (phase 5) and
the tailoring handoff are not built yet.

**You never type a company name.** The board list is discovered, not curated.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/jobhunt init
```

`init` writes `~/.jobhunt/config.yaml` and runs the alembic migrations against
`~/.jobhunt/jobhunt.db`. The boards table starts empty.

Point everything at a scratch directory with `JOBHUNT_HOME=/tmp/whatever`.

## Commands

```bash
jobhunt init                          # config and database
jobhunt discover --strategy yc          # seed boards from a public company list
jobhunt discover --strategy commoncrawl # bulk backfill from the crawl index
jobhunt discover --strategy feeds       # register the tier 2 aggregators
jobhunt discover --strategy harvest     # mine ATS tokens out of ingested URLs
jobhunt discover --domain acme.com    # probe one company I actually care about
jobhunt boards --list --status validated
jobhunt boards --validate             # one fetch per candidate
jobhunt sync                          # every source
jobhunt sync --source ashby           # one source
jobhunt sync --source ashby --force   # ignore next_fetch_at
jobhunt sync --source ashby --dry-run # fetch and normalize, write nothing
jobhunt sync --source ashby --from-raw 20260820T000513   # re-normalize, no network
jobhunt boards                        # counts by provider, status, tier
jobhunt sources                       # per-source health
jobhunt list --source greenhouse --limit 20
jobhunt show <job_id>
jobhunt stats
```

Every command prints one line or one table. Payloads go to disk, never to stdout.

## How a sync works

Two passes over separate storage, which is the point.

1. **Fetch.** Boards due per `next_fetch_at`, capped per run. Each response is
   written verbatim to `~/.jobhunt/data/raw/{source}/{run_key}/{provider}__{token}.json`
   in an envelope carrying the token, market, and fetch time.
2. **Normalize.** Each stored envelope is parsed into `JobPosting` records and
   upserted. `--from-raw <run_key>` replays this pass alone, so a parser bug costs
   a re-parse and never a re-fetch.

Then deduplication runs, boards get their tier and `next_fetch_at` updated, and a
`runs` row records the outcome.

A board that errors does not fail the run: the source is marked `degraded`, the
error is recorded, and the other boards still land. Three consecutive errors and
the board is marked `dead` and never fetched again.

## Discovery: where boards come from

`PLAN.md` 3.5 lists five strategies. Two are built, and the measured yield
decided the order.

**Strategy B plus E, the cold start.** `discover --strategy yc` pulls the
`yc-oss` hiring list (1488 companies), derives a token from each company website
(`inkeep.com` -> `inkeep`), and writes one candidate board per provider. It never
fetches a board itself. The ordinary sync validation loop proves each candidate
with a single request: jobs found means `validated`, a valid empty response means
`empty`, an error means `dead` on the spot.

From an empty database, 40 companies produced 120 candidates, of which 21
validated into live boards carrying 1267 jobs. Nothing was named by hand.

**Strategy A, apply-URL harvesting.** `discover --strategy harvest` runs the
regex bank in `jobhunt/discovery/patterns.py` over every `apply_url`,
`source_url`, and description body in the corpus. It is incremental: a watermark
in the `meta` table means only new rows are scanned, and `--full` rescans after a
pattern change.

Only a structured field can attribute a board to a job's company. A link inside a
description body is as likely to be a "see also" for a different company, and a
wrong `company_id` is worse than none.

**A measured caveat.** `PLAN.md` expects Strategy A to cold-start the corpus off
Himalayas. It cannot. Measured across six aggregators, 463 job records produced
one ATS token, because every aggregator wraps its apply link in its own domain.
The write-up is in `references/sources.md`. Strategy A still earns its place: ATS
postings do leak their own tokens, and phase 6 (LinkedIn via Unipile) supplies
raw employer URLs, which is where it pays off.

**Strategy C, the Common Crawl backfill.** `discover --strategy commoncrawl`
queries the CDX index once per provider and turns the returned URLs into
candidates. Live: 4 providers, 4 requests, 23 seconds, 32491 records, **4106 new
candidate boards**. Subdomain wildcards (`*.recruitee.com`) work natively, which
is what covers the subdomain-keyed providers. Validating the first 200 recruitee
candidates resolved 182 live boards and 3056 jobs, a 91 percent hit rate: these
tokens beat domain guesses because they were real URLs.

Run it at setup and maybe quarterly. It is a backfill, not a daily job, and
Common Crawl asks not to be overloaded.

**Strategy D, certificate transparency, is deliberately not built.** Measured:
crt.sh returns only the provider's own infrastructure subdomains, because
customer boards sit behind a wildcard certificate that names nobody. A board
confirmed live in phase 3 does not appear in its own provider's CT results. The
Common Crawl subdomain patterns cover the same providers properly.

**Board scheduling.** Discovery can produce tens of thousands of tokens, so
nothing iterates the boards table. `sync` selects boards where `next_fetch_at` is
due, newest candidates first, with a hard per-run cap. A board with jobs is
re-fetched weekly, an empty one monthly, and a dead one never.

## Sources

| Provider | Endpoint style | JD in the list response |
|---|---|---|
| greenhouse | JSON, token in path | yes, HTML-escaped on the wire |
| ashby | JSON, token in path | yes |
| lever | bare JSON array | yes, split across four fields |
| recruitee | JSON, token in subdomain | yes, split across two fields |
| smartrecruiters | JSON, paged | **no**, detail fetched lazily in phase 5 |
| personio | XML | yes |

Workable and Workday are deferred: see `references/sources.md` for why.

Tier 2 aggregators, registered as feed boards by `discover --strategy feeds` and
re-fetched daily rather than weekly:

| Feed | Records per run | Notes |
|---|---|---|
| remotive | 17 | `limit` is not honoured; salary is free text |
| remoteok | 100 | first array element is a legal notice, not a job |
| arbeitnow | 650 | paged; descriptions are HTML-escaped on the wire |
| jobicy | 50 | best-shaped: structured salary fields |
| wwr | 162 | RSS; title packs "Company: Position" |

None of them leak ATS tokens, so they add jobs, not boards.

## Deduplication

Two layers, per `PLAN.md` section 5.

**Layer 1** is `(source, external_id)`, a unique constraint. A posting already
seen from the same source refreshes its row and bumps `last_seen_at`.

**Layer 2** clusters across sources. Records group by company plus normalized
title, then four checks decide whether two rows are one job:

| Check | Rule |
|---|---|
| country | two known, different countries never merge |
| city | different cities never merge; "New York" and "New York City" do |
| seniority | different known levels never merge |
| description | simhash Hamming distance within 12 over 2-word shingles |

Every check treats an unknown value as "no objection", because aggregators drop
fields the company's own board states fully. Splitting on a missing field is the
failure clustering exists to prevent.

Nothing is deleted. Every source row is kept and `canonical_job_id` points at the
earliest-seen member, so `review` can show a single card with an "also on" line.

## Development

```bash
.venv/bin/pytest tests -q      # 208 tests, no network
.venv/bin/ruff check jobhunt tests scripts
```

Tests never hit the network. Adapter fixtures in `tests/fixtures/` are real
responses captured from the live endpoints, trimmed to five jobs each.

To re-verify endpoints:

```bash
python3 scripts/probe_sources.py            # all 20 endpoints
python3 scripts/probe_sources.py --only ashby
python3 scripts/probe_discovery_yield.py    # how many tokens each aggregator leaks
```

Results land in `data/probe/*.json` and the findings are written up in
`references/sources.md`.

## Schema migrations

```bash
.venv/bin/alembic revision --autogenerate -m "what changed"
.venv/bin/alembic upgrade head
.venv/bin/alembic check
```

The database URL comes from the jobhunt config, never from `alembic.ini`.

## Cold start

```bash
jobhunt init
jobhunt discover --strategy feeds           # 7 aggregator feeds
jobhunt discover --strategy commoncrawl     # thousands of candidate boards
jobhunt discover --strategy yc              # the YC market
jobhunt sync                                # validates candidates as it goes
jobhunt discover --strategy harvest         # mine the URLs that just landed
jobhunt boards --list --status validated
```

Candidates are validated by the ordinary sync, capped per run, so a backfill of
several thousand drains over several runs rather than in one burst.

No company name appears anywhere in that sequence, which is the requirement the
whole discovery layer exists to satisfy.
