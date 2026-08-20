Architecture for `jobhunt`: a local, CLI-only job sourcing and application tracking tool, driven from Claude Code.
 
**Read this document fully before writing any code.** It contains verified endpoint shapes, the data model, the CLI surface, and the integration contract with the existing `tailoring-cv` skill.
 
---
 
## 1. What this is and is not
 
**Is:**
- A Python CLI, run manually or from cron, that pulls job postings from many sources into a local database.
- A ranking layer that surfaces a small daily shortlist instead of a firehose.
- An application tracker where the human records what they actually applied to.
- A trigger that hands an approved job to the existing CV tailoring skill.
**Is not:**
- A web app, a dashboard, an API server, or anything with a frontend.
- An auto-applier. Nothing is ever submitted on the user's behalf.
- A general-purpose scraper framework. Every source is either a public JSON endpoint or a narrowly scoped parser.
**Operating principle:** the tool recommends, the human approves, the system records. Nothing that touches an employer happens without an explicit CLI command from the user.
 
---
 
## 2. Three markets, one pipeline
 
The user targets three distinct markets. They share a schema and a database but have different sources, different filters, and different ranking prompts.
 
| Market key | Meaning | Primary sources |
|---|---|---|
| `yc` | YC-backed and YC-adjacent startups | yc-oss company API resolved to ATS boards, Work at a Startup |
| `global_remote` | Remote roles open to candidates in Turkey / GMT+3 | Himalayas, Remotive, RemoteOK, Arbeitnow, Jobicy, WWR, ATS boards |
| `tr_local` | Roles based in Turkey | LinkedIn, Kariyer.net, İŞKUR, Techcareer, Kodilan, Coderspace |
 
`market` is assigned by the adapter at ingestion time. It is a field on the record, not a separate table. A Turkish company posting a fully remote role can legitimately appear in two market views; handle that through the `remote_type` field and view logic, not by duplicating rows.
 
---
 
## 3. Source tiers
 
Build in tier order. Tier 1 is the bulk of the value and none of the pain.
 
### Tier 1: public ATS JSON endpoints (verified)
 
No auth, no bot protection, documented and intended for third-party consumption. These are the backbone.
 
| Provider | Endpoint | Notes |
|---|---|---|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | No auth for reads. `content=true` includes description HTML plus departments and offices. Single job: `/jobs/{job_id}?questions=true`. Response has `jobs[]` and `meta.total`. Some boards put only work mode in `location.name` and the real city in a `metadata` entry such as "Job Posting Location", so read `metadata` before trusting `location`. |
| Lever | `GET https://api.lever.co/v0/postings/{token}?mode=json` | No auth. Supports `limit`. |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` | No auth. Returns a `workplaceType` enum of `OnSite`, `Remote`, or `Hybrid`, which maps directly onto our `remote_type`. Compensation is unusually well structured here, so Ashby boards are the best source for salary data. Docs expose an LLM-friendly index at `https://developers.ashbyhq.com/llms.txt`. |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings` | No auth. `companyId` is the path segment from `careers.smartrecruiters.com/{companyId}` and is **case sensitive**. The list endpoint returns summaries only; full description requires a per-posting detail call, so fetch detail lazily only for postings that survive the deterministic filter. |
| Recruitee | `GET https://{token}.recruitee.com/api/offers/` | No auth. Returns descriptions and often salary in one call. |
| Personio | `GET https://{company}.jobs.personio.de/xml?language=en` | XML, not JSON. Some tenants are on `.com` instead of `.de`, so resolve the live careers hostname before hardcoding. |
 
**Needs verification before you rely on it.** These are widely reported but I could not confirm the current shape. Probe each with a known company and record the actual response in `references/sources.md` before writing an adapter:
 
- Workable: reported as `https://apply.workable.com/api/v1/widget/accounts/{token}?details=true`. Multiple endpoint generations exist in the wild.
- Workday: reported as `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with a JSON body containing `limit`, `offset`, `searchText`. The `wd{N}` number and site name vary per tenant. High value because large corporates use it, high effort because discovery is messy. Defer to phase 3.
- Teamtailor: has a public XML feed but the customer must enable it and share the URL, and the main API needs a token. Low yield, deprioritize.
### Tier 2: aggregator and remote-board APIs
 
| Source | Endpoint | Status |
|---|---|---|
| Himalayas browse | `GET https://himalayas.app/jobs/api?limit=20&offset={n}` | **Verified.** Max 20 records per request, paginate with `offset`. |
| Himalayas search | `GET https://himalayas.app/jobs/api/search` | **Verified.** Params: `q`, `country`, `worldwide`, `exclude_worldwide`, `seniority` (Entry-level, Mid-level, Senior, Manager, Director, Executive), `employment_type`, `company` (canonical slug), `timezone` (e.g. `UTC+03:00`), `sort` (relevant, recent, salaryAsc, salaryDesc, nameAToZ, nameZToA, jobs), `page`. |
| Himalayas OpenAPI | `https://himalayas.app/docs/openapi.json` | **Verified.** Generate the client from this rather than hand-rolling. |
| Himalayas MCP | `https://himalayas.app/mcp` | **Verified.** No API key. Worth wiring into Claude Code directly as an interactive exploration tool, separate from the batch adapter. |
| Remotive | docs at `https://remotive.com/remote-jobs/api` | Verify endpoint and params before use. |
| RemoteOK | reported `https://remoteok.com/api` | Verify. First array element is typically a legal/attribution notice, not a job. |
| Arbeitnow | reported `https://www.arbeitnow.com/api/job-board-api` | Verify. |
| Jobicy | reported `https://jobicy.com/api/v2/remote-jobs` | Verify. |
| We Work Remotely | category RSS, e.g. `https://weworkremotely.com/categories/remote-programming-jobs.rss` | Verify current category slugs. |
 
**Himalayas attribution requirement:** their terms ask that you link back to the Himalayas URL and credit them as the original source, and explicitly ask that their jobs not be resubmitted to third-party boards. For a private tool this is satisfied by storing and displaying the source URL. Do not build any redistribution on top of it. They rate limit and return 429; back off accordingly.
 
### Tier 3: YC layer
 
The YC company directory is available as a free, daily-refreshed static JSON API. This is the single highest-leverage source for the `yc` market.
 
| Endpoint | Contents |
|---|---|
| `https://yc-oss.github.io/api/meta.json` | Counts and last-updated timestamp |
| `https://yc-oss.github.io/api/companies/all.json` | All launched companies (~5,950) |
| `https://yc-oss.github.io/api/companies/hiring.json` | **Companies where `isHiring` is true. Start here.** |
| `https://yc-oss.github.io/api/batches/{batch}.json` | e.g. `winter-2026`, `spring-2026`, `summer-2025` |
| `https://yc-oss.github.io/api/industries/{slug}.json` | e.g. `b2b`, `infrastructure`, `analytics` |
| `https://yc-oss.github.io/api/tags/{slug}.json` | e.g. `ai`, `generative-ai`, `developer-tools`, `workflow-automation`, `agents`-adjacent tags |
 
Per-company fields include `name`, `slug`, `website`, `batch`, `status`, `stage`, `team_size`, `industries`, `tags`, `regions`, `isHiring`, `one_liner`, `long_description`, and the YC profile `url`. It is generated from YC's own Algolia index by a daily GitHub Action, not scraped from the site.
 
**The YC flow:**
1. Pull `companies/hiring.json`.
2. Filter to relevant tags and batches (recent batches, AI / developer-tools / infrastructure / automation tags, team size under some ceiling if founding-engineer roles are the target).
3. Feed each company `website` into `discover_ats.py` to resolve it to a Tier 1 board.
4. Fetch via the Tier 1 adapter. Most YC companies land on Ashby or Greenhouse.
Only companies that fail ATS resolution need the Work at a Startup path, which requires an authenticated session and should be treated as Tier 4 in terms of fragility.
 
### Tier 4: LinkedIn via Unipile
 
The user already has Unipile wired up from prior work. Reuse that credential and pattern. This is a real API session rather than scraping, and it is the single best source for the `tr_local` market. Rate limit conservatively; a search-heavy pattern on LinkedIn draws attention fast.
 
### Tier 5: Turkish boards (build last)
 
No documented public APIs. These require probing and, in some cases, a headless browser.
 
**Before writing any bespoke HTML parser, try these two things in order:**
 
1. **JSON-LD extraction.** Google requires `schema.org/JobPosting` structured data for a listing to appear in Google Jobs, so most Turkish boards embed a complete JSON-LD block on every job detail page for SEO. Write **one** generic `jsonld_jobposting.py` extractor that pulls `title`, `description`, `datePosted`, `validThrough`, `employmentType`, `hiringOrganization`, `jobLocation`, and `baseSalary` from any page. This collapses five bespoke parsers into one parser plus five thin pagination crawlers. Do this first; it is the highest-leverage decision in the whole Turkish layer.
2. **Mobile app endpoints.** Kariyer.net and İŞKUR both ship official mobile apps, which must talk to a JSON backend. App endpoints are typically far less defended than the web frontend. Probe before reaching for Playwright.
| Source | URL | Approach |
|---|---|---|
| Kariyer.net | `https://www.kariyer.net` | Largest TR board. Aggressive bot protection on web. Probe mobile endpoints, then JSON-LD, then Playwright as last resort. |
| İŞKUR | `https://esube.iskur.gov.tr/istihdam/AcikIsIlanAra.aspx` | Highest raw volume in Turkey. ASP.NET WebForms, no documented API, unprotected but stateful (viewstate). Official İŞKUR Mobil app exists; probe it. |
| Techcareer.net | `https://www.techcareer.net` | Strongest dedicated TR tech board. Modern SPA, very likely has a frontend JSON API. Probe first, expect Tier 1.5. |
| Kodilan | `https://kodilan.com` | Developer-focused, small, clean, remote-friendly. Likely trivial to parse. |
| Coderspace | `https://coderspace.io` | Tech niche plus community. |
| Yenibiriş | `https://www.yenibiris.com` | General white collar. |
| Secretcv | `https://www.secretcv.com` | Mid-market white collar. |
| isbul.net | `https://www.isbul.net` | Free for employers, long-tail SME volume. |
 
**Legal and etiquette constraints, non-negotiable:**
- Kariyer.net and LinkedIn both prohibit automated collection in their terms. This tool is for the user's own job search at human application volume. Never redistribute, resell, or republish collected data.
- Respect `robots.txt` where a source publishes one.
- Identify with a real, contactable User-Agent string. No spoofing a browser to defeat detection.
- Per-source rate limit with jitter. Default 1 request per 2 to 5 seconds for Tier 5, 1 per second for Tier 1.
- Hard cap of a few hundred detail-page fetches per source per run.
- Cache on content hash so repeat runs only fetch genuinely new detail pages.
- Any source that starts returning 403 or a challenge page should be marked degraded and skipped, never retried aggressively.
---
 
## 3.5 Board discovery: never type a company name
 
Tier 1 endpoints are keyed by a **board token**, not a company name. The naive version of this project requires a hand-curated list of companies, which is tedious, always out of date, and biased toward companies the user already knows. That defeats the purpose.
 
**The user must never be required to supply a company name or slug.** Board tokens are discovered automatically by the five strategies below. A manual list exists only as an optional override for a deliberate dream-list.
 
### Strategy A: apply-URL harvesting (build this first)
 
This is the highest-yield strategy and it costs nothing extra, because it runs on data the pipeline already ingests.
 
Every job record from every source carries an `apply_url` and a `source_url`. A large share of them point straight at an ATS board and leak the token in the path or subdomain. Run a regex bank over every URL the system ever sees, from any source including LinkedIn via Unipile:
 
```
https://jobs.ashbyhq.com/{token}/{uuid}                    -> ashby,          {token}
https://boards.greenhouse.io/{token}/jobs/{id}             -> greenhouse,     {token}
https://job-boards.greenhouse.io/{token}/jobs/{id}         -> greenhouse,     {token}
https://jobs.lever.co/{token}/{uuid}                       -> lever,          {token}
https://{token}.recruitee.com/o/{slug}                     -> recruitee,      {token}
https://apply.workable.com/{token}/j/{id}                  -> workable,       {token}
https://careers.smartrecruiters.com/{companyId}/{posting}  -> smartrecruiters,{companyId}
https://{token}.jobs.personio.de/job/{id}                  -> personio,       {token}
https://{tenant}.wd{n}.myworkdayjobs.com/{site}/job/...    -> workday,        {tenant}/{site}
```
 
Also scan for these hosts anywhere in `description_html`, since aggregators frequently embed the real apply link inside the body rather than in a structured field.
 
**Why this compounds.** Every token discovered this way unlocks the company's *entire* board, not just the one job the aggregator carried. A single Himalayas run of 200 jobs typically yields several dozen distinct tokens, and fetching those boards returns hundreds of jobs the aggregator never had, whose own URLs surface more tokens. Discovery becomes a byproduct of ingestion rather than a prerequisite for it. The corpus grows on its own from a cold start with zero manual input.
 
Seed the very first run from Himalayas plus `yc-oss` and the flywheel takes over from there.
 
### Strategy B: yc-oss company list
 
Covered in section 3, Tier 3. Roughly 5,950 companies with `website` and `isHiring`, no names typed by the user. Feed each `website` into the domain prober (Strategy E) or, better, wait for their postings to surface via Strategy A.
 
### Strategy C: Common Crawl CDX index (bulk backfill)
 
**Verified.** Common Crawl publishes a queryable CDX index of every URL it has crawled, free, no key.
 
- Available monthly indices: `https://index.commoncrawl.org/collinfo.json`
- Query pattern: `https://index.commoncrawl.org/CC-MAIN-2025-43-index?url=boards.greenhouse.io%2F*&output=json&fl=url`
- Wildcards work for path prefixes (`example.com/*`) and for subdomains (`*.example.com`).
- Use the `cdx_toolkit` or `cdx-index-client` libraries rather than hand-rolling pagination; they implement the CDX pagination API and parallel page fetching properly.
One pass per provider host yields thousands of historical board tokens in minutes. Two caveats:
 
1. **The data is stale.** Many tokens will be dead boards from companies that folded or switched ATS. Everything from this strategy enters the `boards` table as `candidate` and must be validated before use.
2. **Do not overload the index server.** Common Crawl explicitly asks this. Use the pagination API, cap concurrency, and pull from the S3 bucket instead if the query ever gets large.
Run this once at setup and then maybe quarterly. It is a backfill, not a daily job.
 
### Strategy D: Certificate Transparency (for subdomain-keyed providers)
 
For Recruitee, Personio, and Teamtailor the token **is** a subdomain, which means the full customer list is sitting in public CT logs.
 
```
https://crt.sh/?q=%25.recruitee.com&output=json
https://crt.sh/?q=%25.jobs.personio.de&output=json
https://crt.sh/?q=%25.teamtailor.com&output=json
```
 
The `name_value` field of each record holds the certificate names, newline separated, sometimes with wildcards. Split on newline, strip `*.`, lowercase, deduplicate, and the leftmost label is the board token.
 
crt.sh is slow and rate limits hard, so run it rarely and cache aggressively (weeks, not hours). This does not work for Greenhouse, Lever, Ashby, or SmartRecruiters, where the token lives in a URL path rather than a hostname; those are covered by Strategies A and C.
 
### Strategy E: domain prober (only for companies the user names deliberately)
 
The one place a company name is acceptable is when the user *wants* to target a specific company. `jobhunt discover --domain acme.com` should:
 
1. Fetch `https://{domain}` and look for careers/jobs navigation links. Follow them.
2. On the careers page, look for links, iframes, or script tags pointing at any known ATS host. This catches most embedded boards, including:
   - Greenhouse embed: `boards.greenhouse.io/embed/job_board?for={token}`
   - Ashby embed: `embed.ashbyhq.com/...`
   - Lever embed script referencing `{token}`
3. If nothing is found, guess `token = domain minus TLD minus www` and try it against each provider in turn. This resolves a surprising share of cases for free.
4. Validate: the endpoint returns 200 and a non-empty jobs array.
Store the result on the `companies` row so a company is never probed twice.
 
### The `boards` table
 
Discovery writes here. Fetching reads from here. This table, not a YAML file, is the source of truth.
 
```
id                  INTEGER PK
provider            TEXT      -- greenhouse | lever | ashby | recruitee | ...
token               TEXT
company_id          INTEGER FK, nullable  -- resolved lazily from the first job fetched
discovered_via      TEXT      -- apply_url | yc | commoncrawl | crtsh | domain_probe | manual
discovered_at       TIMESTAMP
status              TEXT      -- candidate | validated | empty | dead
tier                TEXT      -- hot | warm | cold  (see below)
last_fetched_at     TIMESTAMP
next_fetch_at       TIMESTAMP
last_job_count      INTEGER
consecutive_errors  INTEGER
notes               TEXT
```
 
Unique index on `(provider, token)`.
 
**Validation loop.** A `candidate` gets one fetch. Non-empty jobs array becomes `validated`. Valid response with zero jobs becomes `empty`. Three consecutive 404s or errors becomes `dead` and is never fetched again.
 
### Fetch scheduling, and why it is not optional
 
Strategies C and D will produce **tens of thousands** of tokens. Fetching all of them daily would turn a personal job-search tool into a web crawler, which is both rude and slow. Tiering is the control:
 
| Tier | Definition | Cadence |
|---|---|---|
| `hot` | Produced a job that passed the deterministic filter in the last 30 days | Daily |
| `warm` | Has open jobs, but none matched recently | Weekly |
| `cold` | Validated but repeatedly empty | Monthly |
| `dead` | Errored out | Never, prune after 90 days |
 
Drive this off `next_fetch_at` and let `jobhunt sync` simply select boards where `next_fetch_at <= now()` with a hard per-run cap. Tier promotion and demotion happen automatically at the end of each run based on what the board returned.
 
The practical effect: a corpus of 30,000 known boards costs a few hundred requests a day, concentrated on the boards that have actually produced relevant jobs.
 
### What happens to `companies.yaml`
 
It stops being hand-maintained. It becomes a **generated export** of the `boards` table, useful for inspection, diffing, and version control, but never the input.
 
Keep one small hand-editable file, `assets/companies_manual.yaml`, for deliberate additions and for a blocklist. It is merged in at discovery time and its entries are always tiered `hot`.
 
---
 
## 4. Data model
 
SQLite, single file at `~/.jobhunt/jobhunt.db`. SQLite is correct here: one user, one machine, no concurrency, and the whole thing stays greppable and backup-able as a single file. Use SQLAlchemy so a later move to Postgres is a connection-string change.
 
### `companies`
```
id                  INTEGER PK
name                TEXT
normalized_name     TEXT    -- lowercased, legal suffixes and punctuation stripped
domain              TEXT    -- canonical, used as the real identity key
yc_batch            TEXT
yc_slug             TEXT
yc_tags             JSON
team_size           INTEGER
country             TEXT
last_probed_at      TIMESTAMP
probe_status        TEXT    -- resolved | unresolved | error
```
 
ATS provider and token are **not** stored here. They live in the `boards` table defined in section 3.5, because one company can have several boards (multiple Greenhouse tokens for different divisions is common) and because most boards are discovered before their company is even identified.
 
### `jobs`
```
id                  INTEGER PK
external_id         TEXT    -- source-native id
source              TEXT    -- greenhouse | himalayas | unipile_linkedin | kariyernet | ...
market              TEXT    -- yc | global_remote | tr_local
company_id          INTEGER FK
title               TEXT
title_normalized    TEXT    -- seniority and role family extracted
seniority           TEXT    -- intern | junior | mid | senior | staff | lead | principal | founding
role_family         TEXT    -- backend | ai_ml | fullstack | data | devops | pm | other
location_raw        TEXT
country             TEXT
city                TEXT
remote_type         TEXT    -- onsite | hybrid | remote | unknown
tz_min_overlap_h    REAL    -- computed vs Europe/Istanbul, null if unknown
salary_min          NUMERIC
salary_max          NUMERIC
salary_currency     TEXT
salary_period       TEXT    -- hourly | monthly | annual
salary_is_stated    BOOLEAN
description_html    TEXT
description_text    TEXT
description_md      TEXT    -- cleaned markdown, this is what tailoring-cv consumes
description_lang    TEXT    -- ISO code, detected. 'tr' is common in tr_local
description_en_md   TEXT    -- English rendering, populated only when lang != en
jd_completeness     TEXT    -- full | snippet | none
jd_source           TEXT    -- api | jsonld | readability | browser | llm | manual_paste
jd_extracted_at     TIMESTAMP
jd_quality_score    REAL    -- 0..1, heuristic, see section 9.5
description_hash    TEXT    -- simhash, for near-duplicate detection
posted_at           TIMESTAMP
apply_url           TEXT
source_url          TEXT
canonical_job_id    INTEGER -- self-FK, points at the cluster head
first_seen_at       TIMESTAMP
last_seen_at        TIMESTAMP
content_hash        TEXT    -- exact hash, for change detection
is_active           BOOLEAN -- false once it disappears from source
```
 
Unique index on `(source, external_id)`.
Index on `(canonical_job_id)`, `(market, is_active, first_seen_at)`, `(description_hash)`.
 
### `scores`
```
job_id              INTEGER FK
profile             TEXT    -- which ranking profile ran
deterministic_pass  BOOLEAN
deterministic_notes JSON    -- which rules fired, for debugging the filter
llm_score           REAL    -- 0.0 to 1.0
llm_reasoning       TEXT
llm_model           TEXT
scored_at           TIMESTAMP
```
 
### `applications`
This is the human-in-the-loop table and the reason the tool is trustworthy.
```
id                  INTEGER PK
job_id              INTEGER FK
status              TEXT    -- see state machine below
applied_at          TIMESTAMP
folder_path         TEXT    -- path to the tailored CV / cover letter folder
notes               TEXT
skip_reason         TEXT
last_status_change  TIMESTAMP
```
 
### `runs`
```
id                  INTEGER PK
started_at          TIMESTAMP
finished_at         TIMESTAMP
source              TEXT
market              TEXT
raw_fetched         INTEGER
new_jobs            INTEGER
updated_jobs        INTEGER
errors              INTEGER
status              TEXT    -- ok | degraded | failed
error_detail        TEXT
```
 
### Application state machine
 
```
new ──► surfaced ──► interested ──► applied ──► screening ──► interview ──► offer
          │              │             │                          │           │
          └──► skipped   └──► skipped  └──► rejected ◄────────────┴───────────┘
                                       └──► ghosted (auto after N days)
```
 
`skipped` is terminal for that job but records `skip_reason`, which becomes training signal for the ranking prompt over time.
 
---
 
## 5. Deduplication
 
The same role legitimately appears in three places with three different titles. Two layers:
 
**Layer 1, exact identity.** `(source, external_id)`. Used for update detection and `last_seen_at` refresh. Never creates a new row for a posting already seen from the same source.
 
**Layer 2, cross-source clustering.** Compute a grouping key from `(company.domain, title_normalized, country)`. Where that collides, compare `description_hash` simhash distance. Within a Hamming distance threshold, treat as the same job: keep all source rows, set every member's `canonical_job_id` to the earliest-seen row's id.
 
Surface only canonical rows in `review`. Show the alternate sources as a "also on" line so the user can pick the best apply route, which is usually the company's own ATS rather than an aggregator.
 
**Never dedupe on title alone.** "Senior Backend Engineer" at two different companies is two jobs. The company domain must be part of the key.
 
---
 
## 6. Change detection and reposts
 
- `content_hash` over normalized description text plus title plus location.
- On every run, jobs seen get `last_seen_at` bumped. Jobs not seen in a source's full listing for two consecutive successful runs get `is_active = false`.
- A job that goes inactive and later reappears with the same `external_id` is a repost. Track it; frequent reposting is a mild negative signal (role is hard to fill, or the company churns), and worth surfacing in the review output.
- Never delete rows. Historical data is the only way to tune ranking later.
---
 
## 7. Two-stage ranking
 
### Stage 1: deterministic filter (free, runs on everything)
 
Driven by `assets/filters.yaml`, versioned so the user can tune it without touching code.
 
```yaml
profiles:
  yc:
    hard_excludes: ["unpaid", "equity only", "intern", "internship"]
    seniority_min: mid
    boost:
      recent_batch: 1.3          # last 4 batches
      founding_engineer: 1.4
      tag_ai: 1.2
    llm_gate_prompt: prompts/yc_fit.md
    min_score_to_surface: 0.65
 
  global_remote:
    hard_requires:
      remote_type: [remote, hybrid]
    hard_excludes:
      - "US only"
      - "must reside in"
      - "must be located in"
      - "W2"
      - "EST overlap required"
      - "authorized to work in the United States"
    timezone:
      base: "Europe/Istanbul"
      min_overlap_hours: 4
    seniority_min: mid
    llm_gate_prompt: prompts/remote_fit.md
    min_score_to_surface: 0.7
 
  tr_local:
    hard_requires:
      country: TR
    hard_excludes: ["stajyer", "yarı zamanlı", "part time", "çağrı merkezi"]
    seniority_min: mid
    llm_gate_prompt: prompts/tr_fit.md
    min_score_to_surface: 0.6
 
global:
  max_age_days: 30
  exclude_companies: []          # user's own blocklist
  exclude_titles_regex: ["(?i)sales|recruiter|account executive"]
```
 
The timezone overlap rule matters more than it looks. GMT+3 eliminates a large share of US-anchored remote listings, and catching that deterministically before any LLM call is the difference between a $0.30 run and a $15 run.
 
### Stage 2: LLM gate (only on survivors)
 
- Use a cheap model for the gate. Batch 10 to 20 jobs per call with a strict JSON-only response contract.
- Prompt input: the job's title, company one-liner, location, remote type, salary, and a truncated description (first ~1,500 tokens), plus a compact profile summary of the candidate derived from `master.tex`.
- Prompt output: `{job_id, score: 0..1, reasoning: "one sentence", red_flags: []}`.
- Persist reasoning. It is the only way to debug why a good job was buried.
- Hard cap the number of LLM-gated jobs per run and log the estimated cost in the run summary.
---
 
## 8. Project layout
 
```
jobhunt/
  pyproject.toml
  jobhunt/
    __init__.py
    cli.py                    # typer app, thin; all logic lives in modules
    config.py                 # loads ~/.jobhunt/config.yaml + .env
    db/
      models.py               # SQLAlchemy models
      session.py
      migrations/             # alembic
    sources/
      base.py                 # SourceAdapter protocol
      greenhouse.py
      lever.py
      ashby.py
      smartrecruiters.py
      recruitee.py
      personio.py
      himalayas.py
      remotive.py
      remoteok.py
      arbeitnow.py
      jobicy.py
      weworkremotely.py
      yc.py                   # yc-oss company pull + ATS resolution
      unipile_linkedin.py
      tr/
        jsonld.py             # generic schema.org/JobPosting extractor
        kariyernet.py
        iskur.py
        techcareer.py
        kodilan.py
        coderspace.py
    discovery/
      patterns.py             # the ATS URL regex bank, one place, heavily tested
      harvest.py              # Strategy A: pull tokens out of every ingested URL
      commoncrawl.py          # Strategy C: bulk token backfill from the CDX index
      crtsh.py                # Strategy D: CT logs for subdomain-keyed providers
      domain_probe.py         # Strategy E: careers-page probe for a named domain
      validate.py             # candidate -> validated | empty | dead
      schedule.py             # tier promotion/demotion, next_fetch_at
    pipeline/
      normalize.py
      dedupe.py
      rank_deterministic.py
      rank_llm.py
    extract/
      ladder.py               # orchestrates rungs 1-5, records jd_source
      jsonld.py               # rung 1, shared with sources/tr/
      readability.py          # rung 2
      browser.py              # rung 3, Playwright, per-source opt-in
      llm_agent.py            # rung 4, strict schema, extract-never-invent
      quality.py              # the gate, EN + TR heuristics
      translate.py            # description_en_md
      clean.py                # HTML -> markdown, boilerplate stripping
    integrations/
      tailoring.py            # hands off to the tailoring-cv skill
    render/
      review.py               # terminal output, rich
  assets/
    companies.yaml            # GENERATED export of the boards table, do not hand-edit
    companies_manual.yaml     # small hand-edited override list + blocklist
    filters.yaml
    prompts/
      yc_fit.md
      remote_fit.md
      tr_fit.md
  data/
    raw/{source}/{run_id}/    # raw payloads, gitignored
  tests/
```
 
### The adapter contract
 
Every source implements the same protocol. Adding a source must never require touching the pipeline.
 
```python
class SourceAdapter(Protocol):
    source_id: str
    market: str
    rate_limit: RateLimit
 
    def discover(self) -> Iterator[BoardRef]:
        """Yield the boards/queries to fetch. Reads companies.yaml,
        or issues a search, or paginates a listing."""
 
    def fetch(self, ref: BoardRef) -> Iterator[dict]:
        """Yield raw payloads. Writes them to data/raw/ as a side effect.
        Must not transform."""
 
    def normalize(self, raw: dict) -> JobPosting:
        """Raw payload -> canonical record. Pure function, no I/O."""
```
 
Fetch and normalize are separate passes over separate storage. A parser bug must never cost a re-fetch.
 
---
 
## 9. CLI surface
 
Use `typer`. Every command is non-interactive by default with an optional interactive mode, so the whole thing stays cron-able.
 
```
jobhunt init                          # create ~/.jobhunt, db, default configs
jobhunt discover                      # run all enabled discovery strategies
jobhunt discover --strategy harvest   # A: mine tokens from already-ingested URLs
jobhunt discover --strategy yc
jobhunt discover --strategy commoncrawl --provider greenhouse
jobhunt discover --strategy crtsh --provider recruitee
jobhunt discover --domain acme.com    # E: probe one company I actually care about
jobhunt boards                        # counts by provider, status, and tier
jobhunt boards --status candidate --validate   # run the validation loop
jobhunt boards --export               # regenerate assets/companies.yaml
jobhunt sync                          # fetch all boards due per next_fetch_at
jobhunt sync --market yc              # fetch one market
jobhunt sync --source ashby --dry-run
jobhunt rank                          # run both ranking stages on unscored jobs
jobhunt review                        # interactive triage of the shortlist
jobhunt review --market tr_local --limit 20
jobhunt show <job_id>                 # full detail, description rendered
jobhunt jd <job_id>                   # run the extraction ladder for one job
jobhunt jd <job_id> --paste           # paste the JD manually via $EDITOR
jobhunt jd <job_id> --force           # re-extract even if already full
jobhunt jd --queue                    # extract everything queued by the ranker
jobhunt apply <job_id>                # extract if needed, mark applied, trigger tailoring
jobhunt apply <job_id> --no-tailor    # mark applied only
jobhunt skip <job_id> --reason "..."  # mark skipped, feeds future ranking
jobhunt status <job_id> screening     # advance the state machine
jobhunt list --status applied
jobhunt stats                         # applications by week, by market, response rate
jobhunt sources                       # per-source health, last run, degraded flags
```
 
### `jobhunt review` is the main loop
 
For each shortlisted job print a compact card:
 
```
[142] Senior Backend Engineer (AI Infra)          score 0.82
      Parabola · W21 · 24 people · Remote (worldwide)
      $160k-$210k · posted 2d ago · also on: himalayas, remoteok
      "Strong match: FastAPI + LLM orchestration, explicit async-first
       culture, no US-only restriction."
      https://jobs.ashbyhq.com/parabola/...
 
      [a]pply  [s]kip  [d]etail  [l]ater  [q]uit
```
 
`a` runs the apply flow. `s` prompts for a one-line reason. Reasons accumulate and get injected into the LLM gate prompt as negative examples, so the shortlist gets better without any code change.
 
### Non-interactive mode for cron
 
```
jobhunt sync && jobhunt rank && jobhunt review --format=digest --since=1d
```
`--format=digest` writes plain text suitable for piping into an email or a file the user reads over coffee. No LLM call, no prompts, exit 0.
 
---
 
## 9.5 JD extraction
 
The `tailoring-cv` skill takes a **job description** as input. That is the real output of this system: not a link, not a title, but clean JD text good enough to tailor a CV against. Sources differ enormously in how much of that they hand over.
 
| Source class | What the listing call returns | Extraction needed |
|---|---|---|
| Tier 1 ATS APIs | Full description HTML, in the same call | None |
| Himalayas, Remotive, Jobicy | Full description HTML | None |
| RemoteOK, WWR, RSS feeds | Truncated excerpt | Detail fetch |
| LinkedIn via Unipile | Varies by endpoint, often truncated | Detail fetch |
| Kariyer.net, İŞKUR, Techcareer, Yenibiriş, Secretcv | Title, company, snippet | Detail fetch and extraction |
 
So `jd_completeness` starts as `full` for roughly the top half of the table and `snippet` for the rest. Everything below is about closing that gap.
 
### Extraction is lazy, and that is the whole design
 
Do not fetch detail pages during sync. Fetch them at the last responsible moment:
 
1. **Sync time.** Listing data only. Cheap, wide, fast. Many jobs die at the deterministic filter and never need a JD.
2. **Rank time.** The LLM gate runs on whatever description exists. A snippet plus title plus company is usually enough to score fit approximately. If the gate scores a job above the surface threshold and completeness is not `full`, queue it for extraction.
3. **Apply time.** `jobhunt apply` **requires** `jd_completeness == 'full'`. If it is not, it runs the extractor synchronously and blocks until it has real text or fails loudly.
This ordering matters for three reasons. It cuts detail fetches by one to two orders of magnitude. It keeps request volume on the fragile Turkish sources down at a genuinely human level, which is both more defensible and far less likely to trip bot protection. And it means a broken extractor degrades the tool rather than stopping it, because ranking still works off snippets.
 
### The extraction ladder
 
Try each rung in order. Stop at the first that produces text passing the quality gate. Record which rung succeeded in `jd_source`, because that field is how you debug quality complaints later.
 
**Rung 1: JSON-LD.** Parse `<script type="application/ld+json">` for a `JobPosting` object. Google requires this structured data for a listing to appear in Google Jobs, so most job boards, including nearly all the Turkish ones, embed a complete block on every detail page for SEO. Pull `description`, `title`, `datePosted`, `validThrough`, `employmentType`, `hiringOrganization`, `jobLocation`, `baseSalary`. The `description` field is usually the full HTML body. This single rung should cover the large majority of Tier 5, and it is free, deterministic, and stable.
 
**Rung 2: readability extraction.** `trafilatura` or `readability-lxml` on the raw HTML. Strips nav, cookie banners, footers, and related-jobs sidebars, returns the main content block. Good fallback when JSON-LD is absent or truncated.
 
**Rung 3: rendered browser.** Playwright with a persistent context, then retry rungs 1 and 2 against the rendered DOM. Only for sources confirmed to require JS. Slow and heavy, so gate it per source with a config flag rather than trying it everywhere.
 
**Rung 4: the LLM extractor agent.** Last resort, not first. Feed the cleaned text (or, where the page defeats text extraction, a screenshot) to a cheap model with a strict schema and a JSON-only response contract:
 
```json
{
  "description_md": "full job description as clean markdown",
  "responsibilities": ["..."],
  "requirements": ["..."],
  "nice_to_have": ["..."],
  "tech_stack": ["..."],
  "seniority_signal": "senior",
  "language": "tr",
  "salary_text": "...",
  "extraction_confidence": 0.0
}
```
 
Two rules for this rung, both non-negotiable:
 
- **The agent extracts, it never invents.** The prompt must state explicitly that missing fields are returned empty rather than inferred, and that no text may be added that is not present in the source. A hallucinated requirement propagates straight into a tailored CV and a cover letter, which is the single worst failure mode this system has.
- **It is the expensive rung.** Log its usage separately in the run summary. If a source is hitting rung 4 regularly, that is a signal to write a proper parser for it, not to accept the cost.
**Rung 5: manual paste.** When everything fails, `jobhunt jd <id> --paste` opens `$EDITOR` (or reads stdin) so the user pastes the JD from their browser. Sets `jd_source = 'manual_paste'`, completeness `full`. This is a feature, not an admission of defeat: it keeps a Kariyer.net job usable even when the site is actively hostile, and it costs the user fifteen seconds on a job they already decided to apply to.
 
### Quality gate
 
Extracted text passes only if it clears all of these. Otherwise fall through to the next rung.
 
- At least 400 characters of prose after markdown cleanup.
- Contains at least one of: a requirements-like section, a responsibilities-like section, or five or more distinct sentences. Match in both English and Turkish (`gereksinimler`, `aranan nitelikler`, `sorumluluklar`, `iş tanımı`, `nitelikler`).
- Not dominated by boilerplate: reject if more than 60 percent of the text matches known cookie, EEO, or "about the company" template patterns.
- No obvious truncation marker at the end (`...`, `read more`, `devamını oku`, `daha fazla`).
`jd_quality_score` is a simple weighted combination of these signals. Anything below 0.5 gets flagged in `jobhunt apply` with a warning and a suggestion to use `--paste`.
 
### Turkish-language postings
 
The `tr_local` market will produce Turkish JDs while the CV and cover letter are produced in English. Handle it explicitly rather than hoping the tailoring skill copes:
 
- Detect language and store it in `description_lang`.
- When language is not English, produce `description_en_md` via a translation pass. Translate faithfully; do not summarize, do not soften requirements, do not localize job titles into something that sounds better.
- Keep both. The original is what the user reads and what any Turkish-language cover letter would be built from. The English rendering is what feeds an English CV tailoring pass.
- Which one gets written to the tailoring folder is a config setting. See the open question in section 14.
### Caching and re-extraction
 
- Never re-extract when `content_hash` is unchanged. Extraction results are cached against that hash indefinitely.
- Re-extract when the hash changes, since employers do edit postings, sometimes materially.
- Raw fetched HTML for every extraction goes to `data/raw/detail/{source}/{job_id}.html` so a parser fix never requires re-fetching a page from a fragile source.
### Extraction is not scraping-at-scale
 
Worth being explicit, because the rungs above involve fetching pages from sites that prohibit bulk automated collection. Under this design the system fetches a detail page only for a job that has already survived filtering and, at rung 3 and beyond, usually only for a job the user is about to apply to. That is a handful of requests per day at human browsing speed, on postings the user would have opened manually anyway. Keep it that way: per-source rate limits apply to extraction exactly as they do to sync, and there is no bulk detail-fetch command. If one is ever needed, that is a design smell.
 
---
 
## 10. Integration with the existing `tailoring-cv` skill
 
The user already has a skill that reads `master.tex`, tailors a CV to a job, writes a cover letter, and puts both in a per-job folder. **Do not rewrite it. Do not duplicate its logic.** Define a clean contract and call it.
 
### Contract
 
When `jobhunt apply <job_id>` runs:
 
0. **Check `jd_completeness`.** If it is not `full`, run the extraction ladder from section 9.5 now, synchronously. If extraction fails or `jd_quality_score` is below 0.5, stop and tell the user to run `jobhunt jd <id> --paste`. **Never hand a snippet to the tailoring skill.** A CV tailored against a truncated JD is worse than no CV, because it looks finished.
1. Create the folder:
```
   {applications_root}/{YYYY-MM-DD}-{company-slug}-{title-slug}/
```
   `applications_root` comes from `~/.jobhunt/config.yaml`. It must default to the same root the tailoring skill already uses. **Ask the user for this path rather than guessing it.**
 
2. Write the interface files into that folder. These, and only these, are the contract.
   `job.json`:
```json
   {
     "job_id": 142,
     "title": "Senior Backend Engineer (AI Infra)",
     "company": "Parabola",
     "company_domain": "parabola.io",
     "company_one_liner": "...",
     "location": "Remote (worldwide)",
     "remote_type": "remote",
     "market": "yc",
     "seniority": "senior",
     "salary": "$160,000-$210,000 USD/year",
     "posted_at": "2026-08-18",
     "apply_url": "https://jobs.ashbyhq.com/parabola/...",
     "source": "ashby",
     "yc_batch": "W21",
     "match_reasoning": "Strong match: FastAPI + LLM orchestration...",
     "match_score": 0.82,
     "jd_file": "job.md",
     "jd_language": "en",
     "jd_source": "api",
     "jd_quality_score": 0.94,
     "requirements": ["..."],
     "responsibilities": ["..."],
     "tech_stack": ["..."]
   }
```
 
   `job.md`: the full job description as clean markdown, in its original language. **This is the JD the tailoring skill reads.** It must be complete prose, never a snippet or a bullet summary.
 
   `job.en.md`: written only when `jd_language` is not `en`. Faithful English rendering of the same JD.
 
   `jd_file` in `job.json` points at whichever of the two the tailoring skill should consume, resolved from config. That way the skill reads one field and does not need to know the language rules.
 
   The `requirements`, `responsibilities`, and `tech_stack` arrays are populated when rung 4 ran or when the source provided structured data. They are a convenience for the tailoring skill, not a replacement for `job.md`. Omit them rather than guessing.
 
3. Insert the `applications` row with `status = 'applied'`, `applied_at = now()`, `folder_path` set.
4. Invoke the tailoring skill, pointed at the folder. Two supported modes, configurable:
   - `mode: subprocess` — shell out to whatever command the skill exposes, passing the folder path.
   - `mode: handoff` — print a ready-to-paste instruction and stop, e.g. `Tailor my CV for the job in {folder_path}`. This is the safer default until the subprocess interface is confirmed.
5. Never overwrite an existing folder. If the folder exists, warn and stop.
**Ask the user before implementing step 4.** How the tailoring skill is invoked is the one thing this document cannot specify, because it depends on the skill's existing interface. Read the existing `tailoring-cv` SKILL.md first if it is available on disk.
 
### Why the human-in-the-loop step matters technically
 
Recording applications is not just bookkeeping. It:
- Prevents re-surfacing a job the user already acted on, across every source it appears in (the `canonical_job_id` cluster is marked, not just the one row).
- Provides the negative training signal from `skip_reason` that tunes the LLM gate.
- Produces the response-rate stats that tell the user which market is actually converting, which is the only real feedback loop in a job search.
---
 
## 11. The Claude Code skill wrapper
 
The Python package is the engine. The skill is a thin router that tells Claude when and how to drive it.
 
```
~/.claude/skills/job-hunter/
  SKILL.md              # ~150 lines: triggers, workflow, command reference
  references/
    sources.md          # per-source endpoint shapes, quirks, verified status
    schema.md           # canonical record + state machine
    troubleshooting.md  # what to do when a source goes degraded
```
 
**SKILL.md rules:**
- The `description` field is the trigger and the highest-leverage line in the file. Load it with the phrases the user will actually type, in both English and Turkish: job search, job scraping, iş ilanı tarama, new jobs, daily job digest, apply to job, başvuru, YC jobs, remote jobs, Kariyer.net.
- The body routes to scripts. It must not contain scraping logic, endpoint URLs, or parsing instructions. Those live in `references/` and are read on demand.
- Every script writes JSON to disk and prints a one-line summary. Claude reads summaries, not payloads. This is what keeps a 400-job run from consuming the context window.
- Explicitly instruct: never run `jobhunt apply` without the user naming the job. The tool records real-world actions and must never act on inference.
---
 
## 12. Build order
 
Do not skip ahead. Each phase must run end to end before the next starts.
 
**Phase 1 — skeleton and schema**
`init`, SQLAlchemy models, alembic, config loading, the `SourceAdapter` protocol, and the dedupe logic tested against synthetic duplicates. No network calls yet.
 
**Phase 2 — first two adapters**
Greenhouse and Ashby. Cleanest shapes, best data, no auth. Prove fetch → raw → normalize → dedupe → store works. Seed with a handful of tokens by hand purely to have test data; they get thrown away in phase 3. Add `jobhunt sources` health output.
 
**Phase 3 — the discovery flywheel**
`boards` table, `patterns.py` regex bank with tests, Strategy A harvesting, the validation loop, and tier scheduling. Then Himalayas as the seed source, since it is verified and well documented. Run `sync → harvest → sync` and confirm the second sync fetches boards that nothing told it about. **This is the phase that removes manual company lists, and it is the one to get right.** Add Lever, SmartRecruiters, Recruitee, Personio adapters, because harvesting will immediately surface tokens for all of them.
 
**Phase 4 — seed expansion**
`yc-oss` pull for the `yc` market. Then Common Crawl and crt.sh backfills. Then verify and add Remotive, RemoteOK, Arbeitnow, Jobicy, WWR as additional seed and harvest sources. By the end of this phase the boards table should hold thousands of validated tokens, none of them typed by hand.
 
**Phase 5 — ranking, review, and JD extraction**
`filters.yaml`, deterministic filter, LLM gate, `jobhunt review`, then the extraction ladder rungs 1, 2, and 5 (JSON-LD, readability, manual paste), then `jobhunt apply` with the tailoring handoff. Rungs 3 and 4 can wait; at this stage every source in the corpus already returns full descriptions, so the ladder mostly exists to enforce the completeness gate. **The tool becomes useful here.** Run it daily for a week before building anything else, and tune `filters.yaml` against real data.
 
**Phase 6 — LinkedIn via Unipile**
Reuse the existing Unipile credential and patterns. Unlocks the bulk of `tr_local`.
 
**Phase 7 — Turkish boards**
This is where the extraction ladder earns its keep, because these sources return snippets and the JD lives on a detail page. Add rung 3 (browser) and rung 4 (LLM extractor), plus `translate.py`. Then Techcareer and Kodilan (likely easy, probably JSON-LD). Then İŞKUR. Kariyer.net last, and only if the earlier sources have not already produced enough TR volume. Measure rung usage per source: if a source needs rung 4 more than occasionally, write it a real parser instead.
 
---
 
## 13. Non-negotiables
 
1. **No auto-apply. Ever.** No form submission, no message sending, no automated outreach to any employer.
2. **Fetch and parse are separate passes.** Raw payloads are always persisted before normalization.
3. **Every source is isolated.** A failing source marks itself degraded in `runs` and the pipeline continues. One broken parser never fails a run.
4. **Deterministic filter always runs before the LLM gate.** No exceptions, no "just this once" full-corpus LLM passes.
5. **No secrets in the repo.** `.env` for Unipile and LLM keys, `.gitignore` covers `data/`, `.env`, `*.db`.
6. **Every script is standalone.** Runnable from a shell without Claude, testable in isolation.
7. **The user never has to supply a company name or slug** to get jobs from a board. Discovery is automatic. `companies_manual.yaml` is an optional override, never a requirement, and the tool must work fully with that file empty.
8. **Every board fetch goes through `next_fetch_at` scheduling.** No command ever iterates the full boards table. A per-run request cap is enforced and logged.
9. **The tailoring skill never receives a partial JD.** `jobhunt apply` hard-blocks on `jd_completeness == 'full'` and a quality score above 0.5.
10. **The LLM extractor extracts, it never invents.** Missing fields come back empty. No inferred requirements, no reconstructed sentences, no filled-in gaps.
11. **Detail pages are fetched lazily, one job at a time.** There is no bulk detail-fetch command and there never will be.
12. **No em-dashes in any generated output** (CLI text, digests, cover letters, docs). House style.
13. **Errors are loud in logs, quiet in the digest.** The daily read should be jobs, not stack traces.
---
 
## 14. Open questions to ask the user before coding
 
Do not guess these. Ask, then proceed.
 
1. What is the absolute path to `master.tex` and to the applications root folder the `tailoring-cv` skill already writes into?
2. How is the `tailoring-cv` skill invoked today: a slash command, a natural-language request, or a script? This determines subprocess vs handoff mode.
3. **What exactly does `tailoring-cv` expect as JD input:** a file path, raw text on stdin, a specific filename it looks for in the folder, or a structured object? Section 10 assumes `job.md` in the folder, but match the skill's real interface instead of forcing it to change.
4. For Turkish postings, should the tailoring skill receive the Turkish original or the English translation? And should cover letters for Turkish companies be written in Turkish or English?
5. Which LLM and which API key should the ranking gate and the rung 4 extractor use? They can differ; the extractor benefits from a stronger model than the gate.
6. Roughly what daily volume is wanted in the digest: 5 jobs, 15, 50?
7. Are Turkish-language postings acceptable in the shortlist, or should everything surfaced be English?
8. Is there an existing Postgres instance this should eventually write to, or is SQLite the permanent home?
