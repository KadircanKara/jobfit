"""Typer app. Thin by design: all logic lives in modules, so every path is
reachable from a shell and testable without the CLI.
"""
from __future__ import annotations

import json
import pathlib
import sys

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from jobhunt import __version__, store, sync
from jobhunt import applications as applications_module
from jobhunt import config as config_module
from jobhunt import preferences as preferences_module
from jobhunt import sources as source_registry
from jobhunt.db.models import Application, Board, Company, Job, Run, Score
from jobhunt.db.session import ensure_current, session_scope, upgrade_to_head
from jobhunt.discovery import commoncrawl as cc_module
from jobhunt.discovery import feeds as feeds_module
from jobhunt.discovery import harvest as harvest_module
from jobhunt.discovery import patterns
from jobhunt.discovery import yc as yc_module
from jobhunt.extract import ladder as ladder_module
from jobhunt.rank import deterministic as rank_filters
from jobhunt.rank import runner as rank_runner
from jobhunt.render import csv_export
from jobhunt.render import review as review_render

app = typer.Typer(add_completion=False, help="Local job sourcing and application tracking.")
console = Console()


_SCHEMA_CHECKED = False


def _config() -> config_module.Config:
    """Load config, and bring the database to head if the package moved ahead.

    Checked once per process. Every command below assumes a current schema, and
    a stale one surfaces as a raw SQLite error that says nothing useful.
    """
    global _SCHEMA_CHECKED
    cfg = config_module.load()
    if not _SCHEMA_CHECKED:
        _SCHEMA_CHECKED = True
        try:
            if ensure_current(cfg.db_path):
                console.print("db: schema upgraded to head")
        except Exception as exc:  # noqa: BLE001 - report, never block the command
            console.print(f"db: could not check schema ({type(exc).__name__}: {exc})")
    return cfg


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Rewrite an existing config file.")) -> None:
    """Create ~/.jobhunt, the database, and the default config."""
    path = config_module.write_default(force=force)
    cfg = config_module.load(path)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(cfg.db_path)
    installed = rank_filters.install_user_copies(cfg)
    console.print(f"init: config={path} db={cfg.db_path} data={cfg.data_dir}")
    if installed:
        console.print(f"init: wrote {len(installed)} tunable files, starting with {installed[0]}")
    console.print("next: `jobhunt discover --strategy yc` seeds boards from a public list.")


@app.command("sync")
def sync_cmd(
    source: str = typer.Option(None, "--source", help="Source id. Omit to run every source."),
    market: str = typer.Option(None, "--market", help="Restrict to one market: yc, global_remote, tr_local."),
    force: bool = typer.Option(False, "--force", help="Ignore next_fetch_at scheduling."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch and normalize, write nothing."),
    fast: bool = typer.Option(
        False, "--fast", help="Feeds plus a small slice of boards. Minutes, not half an hour."
    ),
    max_boards: int = typer.Option(None, "--max-boards", help="Cap board fetches this run."),
    from_raw: str = typer.Option(
        None, "--from-raw", help="Re-normalize a stored run key without re-fetching."
    ),
) -> None:
    """Fetch due boards, normalize, dedupe, store."""
    cfg = _config()
    if not cfg.db_path.exists():
        console.print("sync: no database. run `jobhunt init` first.")
        raise typer.Exit(1)

    cap = max_boards or (sync.FAST_MAX_BOARDS if fast else None)
    if cap:
        cfg.raw.setdefault("sync", {})["max_boards_per_run"] = cap

    targets = [source] if source else sorted(source_registry.REGISTRY)
    for name in targets:
        if name not in source_registry.REGISTRY:
            known = ", ".join(sorted(source_registry.REGISTRY))
            console.print(f"sync: unknown source {name!r}. known: {known}")
            raise typer.Exit(2)

    worst = "ok"
    lock = sync.SyncLock(cfg)
    with lock:
        if not lock.acquired and from_raw is None:
            console.print("sync: another sync is running, skipping this pass")
            raise typer.Exit(0)
        worst = _sync_targets(cfg, targets, force, dry_run, from_raw, market)
    raise typer.Exit(1 if worst == "failed" else 0)


def _sync_targets(cfg, targets, force, dry_run, from_raw, market) -> str:
    worst = "ok"
    for name in targets:
        try:
            result = sync.sync_source(
                cfg, name, force=force, dry_run=dry_run, from_raw=from_raw, market=market
            )
        except Exception as exc:  # noqa: BLE001 - a stack trace is for the log, not the terminal
            console.print(f"{name}: failed {type(exc).__name__}: {exc}")
            worst = "failed"
            continue
        console.print(result.summary())
        if result.error_detail and result.status != "ok":
            console.print(f"  first error: {result.error_detail.splitlines()[0]}")
        if result.status == "failed":
            worst = "failed"
        elif result.status == "degraded" and worst == "ok":
            worst = "degraded"
    return worst


STRATEGIES = ("harvest", "yc", "commoncrawl", "feeds")


@app.command()
def discover(
    strategy: str = typer.Option("harvest", "--strategy", help=f"One of: {', '.join(STRATEGIES)}."),
    domain: str = typer.Option(None, "--domain", help="Probe one company I actually care about."),
    full: bool = typer.Option(False, "--full", help="Rescan every job, ignoring the watermark."),
    limit: int = typer.Option(None, "--limit", help="Stop after this many records."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be written."),
    from_raw: bool = typer.Option(False, "--from-raw", help="Re-parse the stored list, no fetch."),
    market: str = typer.Option(None, "--market", help="Override the market these boards belong to."),
    provider: str = typer.Option(
        None, "--provider", help="Restrict commoncrawl to one provider. Repeat with commas."
    ),
    max_pages: int = typer.Option(
        cc_module.MAX_PAGES, "--max-pages", help="CDX pages per provider. Each is ~1 MB."
    ),
    run_key: str = typer.Option(None, "--run-key", help="Re-parse a stored commoncrawl run."),
    no_guess: bool = typer.Option(
        False, "--no-guess", help="Skip token guesses from employer-hosted URLs."
    ),
) -> None:
    """Run a discovery strategy. Writes boards, never companies typed by hand."""
    cfg = _config()
    if domain:
        _discover_domain(cfg, domain, dry_run)
        return
    if strategy not in STRATEGIES:
        console.print(f"discover: unknown strategy {strategy!r}. known: {', '.join(STRATEGIES)}")
        raise typer.Exit(2)

    if strategy == "feeds":
        registered = feeds_module.seed(cfg, dry_run=dry_run)
        console.print(registered.summary())
        return

    if strategy == "commoncrawl":
        providers = tuple(p.strip() for p in provider.split(",")) if provider else cc_module.DEFAULT_PROVIDERS
        try:
            backfill = cc_module.run(
                cfg, providers=providers, market=market or "global_remote",
                max_pages=max_pages, dry_run=dry_run, from_raw=run_key,
            )
        except Exception as exc:  # noqa: BLE001 - a strategy is isolated like a source
            console.print(f"commoncrawl: failed {type(exc).__name__}: {exc}")
            raise typer.Exit(1) from None
        console.print(backfill.summary())
        console.print("next: `jobhunt sync` validates the candidates, one request each.")
        return

    if strategy == "yc":
        try:
            seeded = yc_module.run(
                cfg, limit=limit, dry_run=dry_run, from_raw=from_raw,
                market=market or yc_module.MARKET,
            )
        except Exception as exc:  # noqa: BLE001 - a strategy is isolated like a source
            console.print(f"yc: failed {type(exc).__name__}: {exc}")
            raise typer.Exit(1) from None
        console.print(seeded.summary())
        console.print("next: `jobhunt sync` validates the candidates, one request each.")
        return

    result = harvest_module.harvest(
        cfg, full=full, limit=limit, dry_run=dry_run, guess_from_hints=not no_guess
    )
    console.print(result.summary())
    if not dry_run:
        console.print(f"report: {harvest_module.write_report(cfg, result)}")


def _discover_domain(cfg, domain: str, dry_run: bool) -> None:
    """Strategy E for one company. The only place a name is acceptable is here."""
    clean = yc_module.domain_of(domain) or domain
    token = patterns.guess_token(clean)
    if not token:
        console.print(f"discover: cannot derive a token from {domain!r}")
        raise typer.Exit(2)

    added = []
    with session_scope(cfg.db_path) as session:
        for provider in yc_module.GUESS_PROVIDERS:
            existing = session.scalars(
                select(Board).where(Board.provider == provider, Board.token == token)
            ).first()
            if existing is not None:
                continue
            added.append(provider)
            if dry_run:
                continue
            board = store.get_or_create_board(session, provider, token, "domain_probe", "global_remote")
            board.status = "candidate"
            board.notes = f"guessed from {clean}"
    console.print(f"discover: token={token} candidates_added={len(added)} {' '.join(added) or '-'}")
    if added and not dry_run:
        _validate_candidates(cfg, None)


@app.command()
def rank(
    market: str = typer.Option(None, "--market", help="Restrict to one market."),
    limit: int = typer.Option(None, "--limit", help="Stop after this many jobs."),
    rescore: bool = typer.Option(False, "--rescore", help="Re-run stage 1 on already-scored jobs."),
    emit: str = typer.Option(None, "--emit", help="Write an LLM gate batch to this path."),
    ingest: str = typer.Option(None, "--ingest", help="Read gate verdicts back from this path."),
    batch: int = typer.Option(None, "--batch", help="Jobs per emitted batch."),
) -> None:
    """Stage 1 deterministic filter, and the file protocol for the stage 2 gate."""
    cfg = _config()

    if ingest:
        path = pathlib.Path(ingest).expanduser()
        if not path.exists():
            console.print(f"rank: no such file {path}")
            raise typer.Exit(1)
        console.print(rank_runner.ingest(cfg, path).summary())
        return

    if emit is not None:
        size = batch or int(cfg.get("ranking", "batch_size", default=rank_runner.DEFAULT_BATCH))
        target = pathlib.Path(
            emit or str(cfg.get("ranking", "batch_path", default=cfg.data_dir / "rank/batch.json"))
        ).expanduser()
        written = rank_runner.emit(cfg, target, market=market, limit=size)
        console.print(f"rank emit: batches={written['batches']} jobs={written['jobs']} -> {target}")
        if written["jobs"]:
            console.print("next: score them in Claude Code, then `jobhunt rank --ingest <verdicts>`")
        return

    console.print(rank_runner.run_deterministic(cfg, market=market, limit=limit, rescore=rescore).summary())


@app.command()
def boards(
    provider: str = typer.Option(None, "--provider"),
    status: str = typer.Option(None, "--status"),
    market: str = typer.Option(None, "--market"),
    validate: bool = typer.Option(
        False, "--validate", help="Fetch candidate boards once to prove them out."
    ),
    show_list: bool = typer.Option(False, "--list", help="List rows instead of counts."),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Counts by provider, status, and tier."""
    cfg = _config()
    if validate:
        _validate_candidates(cfg, provider, market)
        return
    if show_list:
        _list_boards(cfg, provider, status, limit)
        return
    with session_scope(cfg.db_path) as session:
        stmt = select(Board.provider, Board.status, Board.tier, func.count(Board.id))
        if provider:
            stmt = stmt.where(Board.provider == provider)
        if status:
            stmt = stmt.where(Board.status == status)
        if market:
            stmt = stmt.where(Board.market == market)
        rows = session.execute(stmt.group_by(Board.provider, Board.status, Board.tier)).all()

    table = Table("provider", "status", "tier", "count")
    for row in sorted(rows):
        table.add_row(row[0], row[1], row[2], str(row[3]))
    console.print(table)


@app.command()
def sources() -> None:
    """Per-source health: last run, counts, degraded flags."""
    cfg = _config()
    with session_scope(cfg.db_path) as session:
        job_counts = dict(
            session.execute(
                select(Job.source, func.count(Job.id)).where(Job.is_active.is_(True)).group_by(Job.source)
            ).all()
        )
        latest = {}
        for run in session.scalars(select(Run).order_by(Run.id.desc()).limit(200)).all():
            latest.setdefault(run.source, run)

    table = Table("source", "active jobs", "last run", "status", "fetched", "new", "errors")
    for name in sorted(source_registry.REGISTRY):
        run = latest.get(name)
        table.add_row(
            name,
            str(job_counts.get(name, 0)),
            run.finished_at.strftime("%Y-%m-%d %H:%M") if run and run.finished_at else "never",
            run.status if run else "-",
            str(run.raw_fetched) if run else "-",
            str(run.new_jobs) if run else "-",
            str(run.errors) if run else "-",
        )
    console.print(table)


@app.command("list")
def list_jobs(
    source: str = typer.Option(None, "--source"),
    market: str = typer.Option(None, "--market"),
    limit: int = typer.Option(20, "--limit"),
    canonical_only: bool = typer.Option(True, "--canonical-only/--all-rows"),
) -> None:
    """List stored jobs. Phase 2 has no ranking, so this is newest first."""
    cfg = _config()
    with session_scope(cfg.db_path) as session:
        stmt = (
            select(Job, Company)
            .join(Company, Job.company_id == Company.id, isouter=True)
            .where(Job.is_active.is_(True))
        )
        if source:
            stmt = stmt.where(Job.source == source)
        if market:
            stmt = stmt.where(Job.market == market)
        if canonical_only:
            stmt = stmt.where((Job.canonical_job_id == Job.id) | (Job.canonical_job_id.is_(None)))
        rows = session.execute(stmt.order_by(Job.first_seen_at.desc(), Job.id.desc()).limit(limit)).all()

        table = Table("id", "title", "company", "loc", "mode", "salary", "jd", "src")
        for job, company in rows:
            salary = "-"
            if job.salary_is_stated and job.salary_min:
                high = int(job.salary_max or job.salary_min)
                salary = f"{int(job.salary_min):,}-{high:,} {job.salary_currency or ''}".strip()
            table.add_row(
                str(job.id),
                job.title[:52],
                (company.name if company else "?")[:22],
                (job.city or job.country or job.location_raw or "-")[:18],
                job.remote_type,
                salary,
                job.jd_completeness,
                job.source,
            )
    console.print(table)


@app.command()
def review(
    market: str = typer.Option(None, "--market"),
    limit: int = typer.Option(None, "--limit"),
    since: int = typer.Option(None, "--since", help="Only jobs first seen in the last N days."),
    output_format: str = typer.Option(
        "interactive", "--format", help="interactive or digest. digest is cron-safe."
    ),
    include_unscored: bool = typer.Option(
        False, "--include-unscored", help="Show stage 1 survivors that the gate has not seen."
    ),
) -> None:
    """Triage the shortlist. --format=digest makes no prompts and exits 0."""
    cfg = _config()
    count = limit or int(cfg.get("digest", "limit", default=15))
    cards = review_render.shortlist(
        cfg, market=market, limit=count, since_days=since, include_unscored=include_unscored
    )

    if output_format == "digest":
        # Deliberately plain print, not rich: this gets piped into a file.
        print(review_render.render_digest(cards), end="")
        return
    if output_format != "interactive":
        console.print(f"review: unknown format {output_format!r}. use interactive or digest.")
        raise typer.Exit(2)

    if not cards:
        console.print("review: nothing above threshold. try `jobhunt rank --emit`.")
        return
    _review_loop(cfg, cards)


def _review_loop(cfg, cards) -> None:
    """The main loop. Every action is the user's; nothing happens on its own."""
    for index, card in enumerate(cards, start=1):
        console.print("")
        console.print(f"[dim]{index}/{len(cards)}[/dim]")
        console.print(review_render.render_card(card))
        choice = typer.prompt("      [a]pply [s]kip [d]etail [l]ater [q]uit", default="l").strip().lower()

        if choice.startswith("q"):
            return
        if choice.startswith("d"):
            show(card.job_id)
            choice = typer.prompt("      [a]pply [s]kip [l]ater", default="l").strip().lower()
        if choice.startswith("a"):
            _apply_job(cfg, card.job_id, tailor=True, dry_run=False)
        elif choice.startswith("s"):
            reason = typer.prompt("      reason").strip()
            try:
                with session_scope(cfg.db_path) as session:
                    applications_module.skip(session, card.job_id, reason)
                console.print(f"      skipped {card.job_id}")
            except applications_module.ApplyBlocked as exc:
                console.print(f"      {exc}")


@app.command()
def jd(
    job_id: int,
    paste: bool = typer.Option(False, "--paste", help="Paste the JD from stdin or $EDITOR."),
    force: bool = typer.Option(False, "--force", help="Re-extract even if already full."),
    queue: bool = typer.Option(False, "--queue", help="Extract everything the ranker queued."),
) -> None:
    """Run the extraction ladder for one job. Rungs 1, 2, and 5."""
    cfg = _config()
    if queue:
        _extract_queue(cfg)
        return
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        if job is None:
            console.print(f"jd: no job {job_id}")
            raise typer.Exit(1)
        if paste:
            text = _read_pasted_jd()
            result = ladder_module.paste(session, job, text)
        else:
            result = ladder_module.run(cfg, session, job, force=force)
        console.print(result.summary())
        if result.completeness != "full":
            console.print(f"  not usable yet. try `jobhunt jd {job_id} --paste`.")


def _read_pasted_jd() -> str:
    """$EDITOR when there is a terminal, stdin when there is not, so this stays
    usable from a pipe as well as by hand."""
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return typer.edit("\n# Paste the job description above. Lines starting with # are kept.\n") or ""


def _extract_queue(cfg) -> None:
    """Every stage 1 survivor whose JD is not full yet.

    Bounded by the digest limit rather than unbounded: PLAN.md non-negotiable 10
    forbids a bulk detail-fetch command, and this is the closest thing to one.
    """
    limit = int(cfg.get("digest", "limit", default=15))
    with session_scope(cfg.db_path) as session:
        rows = session.execute(
            select(Job)
            .join(Score, Score.job_id == Job.id)
            .where(Job.is_active.is_(True))
            .where(Score.deterministic_pass.is_(True))
            .where(Job.jd_completeness != "full")
            .order_by(Score.llm_score.desc().nullslast(), Job.id.desc())
            .limit(limit)
        ).scalars().all()
        if not rows:
            console.print("jd queue: nothing to extract.")
            return
        for job in rows:
            console.print(ladder_module.run(cfg, session, job).summary())


@app.command()
def apply(
    job_id: int,
    no_tailor: bool = typer.Option(False, "--no-tailor", help="Record only, no handoff."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be written."),
) -> None:
    """Record that you applied, and hand the folder to the tailoring skill.

    This never submits anything to an employer. You applied; this records it.
    """
    _apply_job(_config(), job_id, tailor=not no_tailor, dry_run=dry_run)


def _apply_job(cfg, job_id: int, tailor: bool, dry_run: bool) -> None:
    try:
        with session_scope(cfg.db_path) as session:
            result = applications_module.apply(
                cfg, session, job_id, tailor=tailor, dry_run=dry_run
            )
    except applications_module.ApplyBlocked as exc:
        console.print(f"apply: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - a stack trace is for the log, not the terminal
        console.print(f"apply: failed {type(exc).__name__}: {exc}")
        return
    console.print(result.summary())
    if result.extraction is not None:
        console.print(f"  {result.extraction.summary()}")
    if tailor and result.mode == "handoff":
        console.print("")
        console.print(f"  [bold]{result.instruction}[/bold]")


@app.command()
def skip(
    job_id: int,
    reason: str = typer.Option(..., "--reason", help="Why. This tunes the gate over time."),
) -> None:
    """Mark a job skipped. The reason is the point, so it is required."""
    cfg = _config()
    try:
        with session_scope(cfg.db_path) as session:
            applications_module.skip(session, job_id, reason)
    except applications_module.ApplyBlocked as exc:
        console.print(f"skip: {exc}")
        raise typer.Exit(1) from None
    console.print(f"skipped: job={job_id} reason={reason!r}")


@app.command()
def status(
    job_id: int,
    new_status: str = typer.Argument(..., metavar="STATUS"),
    note: str = typer.Option(None, "--note"),
) -> None:
    """Advance an application: screening, interview, offer, rejected, ghosted."""
    cfg = _config()
    try:
        with session_scope(cfg.db_path) as session:
            row = applications_module.advance(session, job_id, new_status, note)
            console.print(f"status: job={job_id} -> {row.status}")
    except applications_module.ApplyBlocked as exc:
        console.print(f"status: {exc}")
        raise typer.Exit(1) from None



config_app = typer.Typer(help="Search preferences. Written by the /jobhunt-config wizard.")
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show(
    as_json: bool = typer.Option(False, "--json", help="Machine readable, for the wizard."),
) -> None:
    """Current search preferences."""
    cfg = _config()
    prefs, _ = preferences_module.load(cfg)
    if as_json:
        payload = dict(prefs.as_dict())
        payload["filters_path"] = str(preferences_module.filters_path(cfg))
        print(json.dumps(payload, indent=2))
        return
    table = Table("setting", "value")
    for name, value in prefs.display():
        table.add_row(name, value)
    console.print(table)


@config_app.command("set")
def config_set(
    settings: list[str] = typer.Argument(..., metavar="KEY=VALUE...", help="e.g. titles=backend,AI"),
) -> None:
    """Update preferences and rewrite the managed block of filters.yaml."""
    cfg = _config()
    updates: dict[str, str] = {}
    for item in settings:
        if "=" not in item:
            console.print(f"config: expected KEY=VALUE, got {item!r}")
            raise typer.Exit(2)
        key, _, value = item.partition("=")
        updates[key.strip()] = value

    prefs, _ = preferences_module.load(cfg)
    try:
        prefs = preferences_module.apply_updates(prefs, updates)
        path = preferences_module.save(cfg, prefs)
    except preferences_module.PreferenceError as exc:
        console.print(f"config: {exc}")
        raise typer.Exit(2) from None

    table = Table("setting", "value")
    for name, value in prefs.display():
        table.add_row(name, value)
    console.print(table)
    console.print(f"written: {path}")

    if prefs.titles and cfg.db_path.exists():
        matching, total = preferences_module.title_impact(cfg, prefs)
        share = f"{matching / total:.0%}" if total else "-"
        console.print(f"titles match {matching:,} of {total:,} active jobs ({share})")
        if total and matching / total < 0.02:
            console.print(
                "  that is a narrow list. anything it does not match is never seen, "
                "so widen it if that looks wrong."
            )
    console.print("next: `jobhunt rank --rescore` applies the new rules to the corpus.")


@app.command()
def shortlist(
    market: str = typer.Option(None, "--market"),
    limit: int = typer.Option(None, "--limit"),
    since: int = typer.Option(None, "--since", help="Only jobs first seen in the last N days."),
    include_unscored: bool = typer.Option(False, "--include-unscored"),
    export: bool = typer.Option(False, "--export", help="Upsert into the rolling CSV."),
    output_format: str = typer.Option("table", "--format", help="table or json."),
) -> None:
    """The top matches, as a table, and optionally into the CSV."""
    cfg = _config()
    count = limit or int(cfg.get("digest", "limit", default=15))
    cards = review_render.shortlist(
        cfg, market=market, limit=count, since_days=since, include_unscored=include_unscored
    )

    result = csv_export.export(cfg, cards) if export else None

    if output_format == "json":
        print(json.dumps({
            "count": len(cards),
            "csv_path": str(result.path) if result else None,
            "jobs": [
                {
                    "job_id": card.job_id, "fit": card.score, "title": card.title,
                    "company": card.company, "location": card.location,
                    "work_model": card.remote_type, "employment_type": card.employment_type,
                    "salary": card.salary, "url": card.apply_url, "source": card.source,
                    "reasoning": card.reasoning, "jd_completeness": card.jd_completeness,
                }
                for card in cards
            ],
        }, indent=2))
        return
    if output_format != "table":
        console.print(f"shortlist: unknown format {output_format!r}. use table or json.")
        raise typer.Exit(2)

    if not cards:
        console.print("shortlist: nothing above threshold yet.")
        if result:
            console.print(result.summary())
        return

    table = Table("id", "fit", "title", "company", "location", "mode", "type", "url")
    for card in cards:
        table.add_row(
            str(card.job_id),
            f"{card.score:.2f}" if card.score is not None else "-",
            card.title[:46],
            card.company[:20],
            card.location[:22],
            card.remote_type,
            (card.employment_type or "-").replace("_", " "),
            card.apply_url or "-",
        )
    console.print(table)
    if result:
        console.print(result.summary())


csv_app = typer.Typer(help="The rolling jobs CSV.")
app.add_typer(csv_app, name="csv")


@csv_app.command("mark-applied")
def csv_mark_applied(
    job_ids: list[int] = typer.Argument(..., metavar="JOB_ID..."),
    cv_status: str = typer.Option(None, "--cv-status", help="cv_pending, cv_ready, or cv_failed."),
    record: bool = typer.Option(
        True, "--record/--no-record", help="Also record the application so it stops resurfacing."
    ),
) -> None:
    """Flip applied to TRUE for these jobs, and record them."""
    cfg = _config()
    path = csv_export.csv_path(cfg)
    marked, missing = csv_export.mark_applied(path, job_ids, cv_status=cv_status)

    recorded: list[int] = []
    if record:
        for job_id in marked:
            try:
                with session_scope(cfg.db_path) as session:
                    applications_module.record_applied(session, job_id)
                recorded.append(job_id)
            except applications_module.ApplyBlocked as exc:
                console.print(f"  job {job_id}: {exc}")

    console.print(
        f"csv: marked={len(marked)} recorded={len(recorded)} "
        f"unknown={','.join(str(i) for i in missing) or '-'} -> {path}"
    )


@csv_app.command("path")
def csv_show_path() -> None:
    """Where the rolling CSV lives."""
    console.print(str(csv_export.csv_path(_config())))


@app.command()
def show(job_id: int) -> None:
    """Full detail for one job, description rendered."""
    cfg = _config()
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        if job is None:
            console.print(f"show: no job {job_id}")
            raise typer.Exit(1)
        company = session.get(Company, job.company_id) if job.company_id else None
        duplicates = session.scalars(
            select(Job).where(Job.canonical_job_id == job.canonical_job_id, Job.id != job.id)
        ).all() if job.canonical_job_id else []

        console.print(f"[bold]{job.title}[/bold]  (id {job.id})")
        console.print(
            f"{company.name if company else '?'} - {job.location_raw or '?'} - {job.remote_type}"
        )
        console.print(
            f"seniority={job.seniority} role={job.role_family} "
            f"market={job.market} source={job.source}"
        )
        if job.salary_is_stated:
            console.print(
                f"salary {job.salary_min}-{job.salary_max} "
                f"{job.salary_currency} {job.salary_period}"
            )
        console.print(f"jd: {job.jd_completeness} via {job.jd_source}  posted {job.posted_at}")
        console.print(f"apply: {job.apply_url}")
        if duplicates:
            console.print("also on: " + ", ".join(f"{d.source}#{d.id}" for d in duplicates))
        console.print("")
        console.print((job.description_md or job.description_text or "(no description)")[:4000])


@app.command()
def stats() -> None:
    """Corpus counts."""
    cfg = _config()
    with session_scope(cfg.db_path) as session:
        jobs = session.scalar(select(func.count(Job.id))) or 0
        active = session.scalar(select(func.count(Job.id)).where(Job.is_active.is_(True))) or 0
        canonical = session.scalar(
            select(func.count(Job.id)).where(Job.is_active.is_(True), Job.canonical_job_id == Job.id)
        ) or 0
        companies = session.scalar(select(func.count(Company.id))) or 0
        boards_count = session.scalar(select(func.count(Board.id))) or 0
        full_jd = session.scalar(
            select(func.count(Job.id)).where(Job.is_active.is_(True), Job.jd_completeness == "full")
        ) or 0
        by_market = session.execute(
            select(Job.market, func.count(Job.id))
            .where(Job.is_active.is_(True))
            .group_by(Job.market)
        ).all()
    console.print(
        f"jobs={jobs} active={active} canonical={canonical} clustered_away={active - canonical} "
        f"companies={companies} boards={boards_count} full_jd={full_jd}"
    )
    console.print("markets: " + (" ".join(f"{m}={c}" for m, c in sorted(by_market)) or "-"))
    _application_stats(cfg)


def _application_stats(cfg) -> None:
    """Applications by status and by week, plus the response rate.

    The response rate is the only real feedback loop in a job search, so it is
    printed even when it is zero.
    """
    with session_scope(cfg.db_path) as session:
        by_status = dict(
            session.execute(
                select(Application.status, func.count(Application.id)).group_by(Application.status)
            ).all()
        )
        rows = session.execute(
            select(Job.market, Application.status)
            .join(Job, Application.job_id == Job.id)
        ).all()
        recent = session.scalars(
            select(Application)
            .where(Application.applied_at.is_not(None))
            .order_by(Application.applied_at.desc())
            .limit(200)
        ).all()

    if not by_status:
        console.print("applications: none recorded yet")
        return

    applied = sum(count for status, count in by_status.items() if status != "skipped")
    responded = sum(
        count for status, count in by_status.items()
        if status in {"screening", "interview", "offer"}
    )
    rate = f"{responded / applied:.0%}" if applied else "-"
    console.print(
        "applications: "
        + " ".join(f"{status}={count}" for status, count in sorted(by_status.items()))
        + f" response_rate={rate}"
    )

    per_market: dict[str, int] = {}
    for market, status in rows:
        if status != "skipped":
            per_market[market] = per_market.get(market, 0) + 1
    if per_market:
        console.print(
            "applied by market: " + " ".join(f"{m}={c}" for m, c in sorted(per_market.items()))
        )

    weeks: dict[str, int] = {}
    for row in recent:
        key = row.applied_at.strftime("%G-W%V")
        weeks[key] = weeks.get(key, 0) + 1
    if weeks:
        recent_weeks = sorted(weeks.items())[-6:]
        console.print("applied by week: " + " ".join(f"{w}={c}" for w, c in recent_weeks))


@app.command()
def version() -> None:
    """Print the version and the resolved paths."""
    cfg = _config()
    console.print(f"jobhunt {__version__} config={cfg.path} db={cfg.db_path}")


def _validate_candidates(cfg, provider: str | None, market: str | None = None) -> None:
    """One fetch per candidate. Non-empty becomes validated, empty becomes cold,
    an error kills the guess outright. PLAN.md 3.5 validation loop.
    """
    targets = [provider] if provider else sorted(source_registry.REGISTRY)
    for name in targets:
        if name not in source_registry.REGISTRY:
            console.print(f"boards: no adapter for {name!r}, nothing to validate")
            continue
        result = sync.sync_source(cfg, name, only_status="candidate", market=market)
        console.print(result.summary())


def _list_boards(cfg, provider: str | None, status: str | None, limit: int) -> None:
    with session_scope(cfg.db_path) as session:
        stmt = select(Board, Company).join(Company, Board.company_id == Company.id, isouter=True)
        if provider:
            stmt = stmt.where(Board.provider == provider)
        if status:
            stmt = stmt.where(Board.status == status)
        rows = session.execute(stmt.order_by(Board.id).limit(limit)).all()

        table = Table("id", "provider", "token", "company", "via", "status", "tier", "jobs")
        for board, company in rows:
            table.add_row(
                str(board.id),
                board.provider,
                board.token[:40],
                (company.name if company else "-")[:24],
                board.discovered_via,
                board.status,
                board.tier,
                str(board.last_job_count if board.last_job_count is not None else "-"),
            )
    console.print(table)


if __name__ == "__main__":
    app()
