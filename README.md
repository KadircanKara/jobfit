# jobhunt

Local CLI job sourcing and application tracking. Full architecture in `PLAN.md`.

Phases 1 and 2 are built: skeleton, schema, adapter protocol, dedupe, and the
Greenhouse and Ashby adapters. Discovery (phase 3), ranking (phase 5), and the
tailoring handoff are not built yet.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/jobhunt init
```

`init` writes `~/.jobhunt/config.yaml`, runs the alembic migrations against
`~/.jobhunt/jobhunt.db`, and seeds the three throwaway fixture boards.

Point everything at a scratch directory with `JOBHUNT_HOME=/tmp/whatever`.

## Commands

```bash
jobhunt init                          # config, database, fixture boards
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
.venv/bin/pytest tests -q      # 92 tests, no network
.venv/bin/ruff check jobhunt tests scripts
```

Tests never hit the network. Adapter fixtures in `tests/fixtures/` are real
responses captured from the live endpoints, trimmed to five jobs each.

To re-verify endpoints:

```bash
python3 scripts/probe_sources.py            # all 20
python3 scripts/probe_sources.py --only ashby
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

## Phase 2 fixture boards

`greenhouse/stripe`, `ashby/ramp`, and `ashby/openai` are hardcoded in
`jobhunt/sync.py` as `FIXTURE_BOARDS`, purely so phase 2 has real data. They are
deleted in phase 3, when Strategy A harvesting starts producing tokens on its own.
Nothing else in the codebase requires a company name.
