"""Typer app. Thin by design: all logic lives in modules, so every path is
reachable from a shell and testable without the CLI.
"""
from __future__ import annotations

import pathlib

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from jobhunt import __version__, store, sync
from jobhunt import config as config_module
from jobhunt import sources as source_registry
from jobhunt.db.models import Board, Company, Job, Run
from jobhunt.db.session import session_scope, upgrade_to_head
from jobhunt.discovery import commoncrawl as cc_module
from jobhunt.discovery import feeds as feeds_module
from jobhunt.discovery import harvest as harvest_module
from jobhunt.discovery import patterns
from jobhunt.discovery import yc as yc_module
from jobhunt.rank import deterministic as rank_filters
from jobhunt.rank import runner as rank_runner

app = typer.Typer(add_completion=False, help="Local job sourcing and application tracking.")
console = Console()


def _config() -> config_module.Config:
    return config_module.load()


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
    from_raw: str = typer.Option(
        None, "--from-raw", help="Re-normalize a stored run key without re-fetching."
    ),
) -> None:
    """Fetch due boards, normalize, dedupe, store."""
    cfg = _config()
    if not cfg.db_path.exists():
        console.print("sync: no database. run `jobhunt init` first.")
        raise typer.Exit(1)

    targets = [source] if source else sorted(source_registry.REGISTRY)
    for name in targets:
        if name not in source_registry.REGISTRY:
            known = ", ".join(sorted(source_registry.REGISTRY))
            console.print(f"sync: unknown source {name!r}. known: {known}")
            raise typer.Exit(2)

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
    raise typer.Exit(1 if worst == "failed" else 0)


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
