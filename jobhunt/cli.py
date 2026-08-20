"""Typer app. Thin by design: all logic lives in modules, so every path is
reachable from a shell and testable without the CLI.
"""
from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from jobhunt import __version__, sync
from jobhunt import config as config_module
from jobhunt import sources as source_registry
from jobhunt.db.models import Board, Company, Job, Run
from jobhunt.db.session import session_scope, upgrade_to_head
from jobhunt.discovery import harvest as harvest_module

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
    seeded = sync.seed_fixture_boards(cfg)
    console.print(
        f"init: config={path} db={cfg.db_path} data={cfg.data_dir} "
        f"fixture_boards_added={seeded}"
    )


@app.command("sync")
def sync_cmd(
    source: str = typer.Option(None, "--source", help="Source id. Omit to run every source."),
    market: str = typer.Option(None, "--market", help="Restrict to one market."),
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
            result = sync.sync_source(cfg, name, force=force, dry_run=dry_run, from_raw=from_raw)
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
    if market:
        console.print(f"note: --market {market} is recorded but not yet a filter (phase 3).")
    raise typer.Exit(1 if worst == "failed" else 0)


STRATEGIES = ("harvest",)


@app.command()
def discover(
    strategy: str = typer.Option("harvest", "--strategy", help=f"One of: {', '.join(STRATEGIES)}."),
    full: bool = typer.Option(False, "--full", help="Rescan every job, ignoring the watermark."),
    limit: int = typer.Option(None, "--limit", help="Stop after this many jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be written."),
    no_guess: bool = typer.Option(
        False, "--no-guess", help="Skip token guesses from employer-hosted URLs."
    ),
) -> None:
    """Run a discovery strategy. Writes boards, never companies typed by hand."""
    cfg = _config()
    if strategy not in STRATEGIES:
        console.print(f"discover: unknown strategy {strategy!r}. known: {', '.join(STRATEGIES)}")
        raise typer.Exit(2)

    result = harvest_module.harvest(
        cfg, full=full, limit=limit, dry_run=dry_run, guess_from_hints=not no_guess
    )
    console.print(result.summary())
    if not dry_run:
        console.print(f"report: {harvest_module.write_report(cfg, result)}")


@app.command()
def boards(
    provider: str = typer.Option(None, "--provider"),
    status: str = typer.Option(None, "--status"),
    validate: bool = typer.Option(
        False, "--validate", help="Fetch candidate boards once to prove them out."
    ),
    show_list: bool = typer.Option(False, "--list", help="List rows instead of counts."),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Counts by provider, status, and tier."""
    cfg = _config()
    if validate:
        _validate_candidates(cfg, provider)
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
    console.print(
        f"jobs={jobs} active={active} canonical={canonical} clustered_away={active - canonical} "
        f"companies={companies} boards={boards_count} full_jd={full_jd}"
    )


@app.command()
def version() -> None:
    """Print the version and the resolved paths."""
    cfg = _config()
    console.print(f"jobhunt {__version__} config={cfg.path} db={cfg.db_path}")


def _validate_candidates(cfg, provider: str | None) -> None:
    """One fetch per candidate. Non-empty becomes validated, empty becomes cold,
    an error kills the guess outright. PLAN.md 3.5 validation loop.
    """
    targets = [provider] if provider else sorted(source_registry.REGISTRY)
    for name in targets:
        if name not in source_registry.REGISTRY:
            console.print(f"boards: no adapter for {name!r}, nothing to validate")
            continue
        result = sync.sync_source(cfg, name, only_status="candidate")
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
