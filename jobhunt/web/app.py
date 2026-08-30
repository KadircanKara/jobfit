"""The HTTP surface.

Loopback only, single user, no auth. The browser enforces the same rules this
module does, but the browser is a convenience: a broken filter set that reached
the scraper would waste a whole run, so every rule is checked again here.
"""
from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from jobhunt import preferences as prefs_module
from jobhunt.config import Config
from jobhunt.config import load as load_config
from jobhunt.db.models import Board, utcnow
from jobhunt.db.session import session_scope
from jobhunt.discovery import categories as categories_module
from jobhunt.outreach import poller as outreach_poller
from jobhunt.outreach import stub as outreach_stub
from jobhunt.web import agent as agent_module
from jobhunt.web import applied as applied_module
from jobhunt.web import filters as webfilters
from jobhunt.web import history as history_module
from jobhunt.web import idle as idle_module
from jobhunt.web import outreach as outreach_routes
from jobhunt.web import profile as profile_module
from jobhunt.web import revise as revise_module
from jobhunt.web import tailor as tailor_module
from jobhunt.web import vocab as vocab_module
from jobhunt.web.events import EventLog, to_sse
from jobhunt.web.runs import DEFAULT_GATE_ROUNDS, RunSupervisor

STATIC_DIR = pathlib.Path(__file__).parent / "static"


class _BadFeedRequest(Exception):
    """The `approve`/`retire` payload could not be turned into board selections.

    Covers both a provider the vocabulary does not know and every shape
    FastAPI's own `dict[str, Any]` validation does not check: a non-list
    value, a row that is not an object, a provider or token that is not a
    string. All of them are the caller's fault, not the server's, so all of
    them become one clean 422 rather than an unhandled 500 — a non-string
    token in particular would otherwise reach SQLite and raise there.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AppState:
    """What the process holds between requests: one run, one event log."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = EventLog()
        self.supervisor: RunSupervisor | None = None
        self.batch: tailor_module.TailorBatch | None = None
        # Revision sessions outlive the tab, the same way a run does.
        self.desk = revise_module.ReviseDesk(config)
        self.idle = idle_module.IdleClock()

    def busy(self) -> bool:
        """Whether anything would be lost by stopping the process now."""
        if self.supervisor is not None and self.supervisor.state.running:
            return True
        if self.batch is not None and self.batch.running:
            return True
        return bool(self.desk.sessions())

    def state_dict(self) -> dict[str, Any]:
        if self.supervisor is None:
            # No run in this process, but the corpus outlives the process. The
            # shortlist is rebuilt from the database rather than showing an
            # empty screen for work that was already done.
            return {
                "phase": "idle", "running": False, "outcome": _restored_outcome(self.config),
                "error": None, "degraded": [], "counters": {}, "rank": None, "gate": None,
                "run_id": "", "started_at": "", "finished_at": None,
                "resumable": False, "paused": False,
                "results": _last_shortlist(self.config), "last_seq": self.log.latest_seq(),
            }
        state = self.supervisor.state
        return {
            "phase": state.phase,
            "running": state.running,
            "outcome": state.outcome,
            "error": state.error,
            "degraded": list(state.degraded),
            "counters": dict(state.counters),
            "run_id": state.run_id,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "resumable": state.resumable,
            "paused": self.supervisor.pausing and state.running,
            "rank": dataclasses.asdict(state.rank) if state.rank else None,
            "gate": dataclasses.asdict(state.gate, dict_factory=_without_payload)
            if state.gate
            else None,
            # A finished run's list is a snapshot, and the corpus moves under
            # it: a rescore, a gate ingest or an edit to the filters all change
            # what belongs on screen. While the run is still going its own rows
            # are the live ones, so only a finished run is rebuilt.
            "results": list(state.results) if state.running else _last_shortlist(self.config),
            "stopping": self.supervisor.stopping,
            "last_seq": self.log.latest_seq(),
        }


# Marks a state the browser did not watch happen. The hero says so rather than
# claiming a run finished in this session.
RESTORED = "restored"


def _last_shortlist(config: Config) -> list[dict[str, Any]]:
    """The shortlist as it stands on disk, in the shape a run would have left.

    Deliberately the same call the run itself makes, so a restored screen and a
    live one cannot disagree about what is above the bar.
    """
    from jobhunt import preferences as prefs
    from jobhunt.render import review
    from jobhunt.web.engine import NEAR_MISSES, row_from_card

    try:
        wanted, _ = prefs.load(config)
        cards = review.shortlist(config, limit=wanted.top_n)
        rows = [row_from_card(card) for card in cards]
        rows += [
            row_from_card(card) | {"below_bar": True}
            for card in review.near_misses(config, limit=NEAR_MISSES)
        ]
        return rows
    except Exception:
        # A corpus that cannot be read is not a reason to fail the page. The
        # screen simply shows nothing until a run fills it.
        return []


def _restored_outcome(config: Config) -> str | None:
    return RESTORED if _last_shortlist(config) else None


def _row_for(state: AppState, job_id: int) -> dict[str, Any] | None:
    rows = state.batch.state() if state.batch is not None else []
    return next((row for row in rows if row["job_id"] == job_id), None)


def _name_the_rows(config: Config, batch: Any) -> None:
    """Give each row a title, a company and the posting link, so the batch has
    something to call it and a way back to the source. One query for the whole
    batch, before any work starts."""
    from jobhunt.db.models import Company, Job

    ids = [row.job_id for row in batch.rows]
    with session_scope(config.db_path) as session:
        found = session.execute(
            select(Job.id, Job.title, Company.name, Job.apply_url)
            .join(Company, Job.company_id == Company.id, isouter=True)
            .where(Job.id.in_(ids))
        ).all()
    named = {job_id: (title, company, url) for job_id, title, company, url in found}
    for row in batch.rows:
        title, company, url = named.get(row.job_id, ("", "", None))
        row.title = title or ""
        row.company = company or ""
        row.url = url or None


def _run_payload(state: Any, jh: AppState) -> dict[str, Any]:
    """A run, in the shape the browser already reads a live one in.

    Deliberately the same shape as `state_dict`, so looking at a run from
    yesterday goes through the same components as watching one now.
    """
    return {
        "phase": state.phase,
        "running": False,  # a saved run is never the one currently going
        "outcome": state.outcome,
        "error": state.error,
        "degraded": list(state.degraded),
        "counters": dict(state.counters),
        "run_id": state.run_id,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "resumable": state.resumable,
        "paused": False,
        "rank": dataclasses.asdict(state.rank) if state.rank else None,
        "gate": dataclasses.asdict(state.gate, dict_factory=_without_payload)
        if state.gate
        else None,
        "results": list(state.results),
        "last_seq": jh.log.latest_seq(),
    }


def _without_payload(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Drop a batch's payload on the way out.

    It is the whole prompt plus every job description in the batch — tens of
    kilobytes the page polls every two seconds and never reads.
    """
    return {key: value for key, value in pairs if key != "payload"}


def build_pipeline(config: Config, state: AppState) -> Any:
    """The real engine, wired so board progress reaches the event log.

    Separate from the endpoint so tests can substitute a pipeline that does not
    reach the network.
    """
    from jobhunt.web.engine import EnginePipeline

    pipeline = EnginePipeline(config)

    def on_board(source: str, done: int, total: int, token: str) -> None:
        state.log.emit(
            phase="sync",
            source=source,
            message=f"{source} board {done}/{total} · {token}",
            boards_done=done,
            boards_total=total,
        )

    def on_rank(report: Any) -> None:
        # Stage 1 reports itself while it runs. It goes to the state the page
        # polls rather than to the event log: at one line per fifty jobs a
        # four-thousand job pass would bury every other message in the feed.
        if state.supervisor is not None:
            state.supervisor.state.rank = report

    pipeline.on_board = on_board
    pipeline.on_rank = on_rank
    pipeline.should_stop = lambda: bool(state.supervisor and state.supervisor.stopping)
    return pipeline


def _filters_payload(config: Config) -> dict[str, Any]:
    prefs, _ = prefs_module.load(config)
    return dataclasses.asdict(prefs) | {"max_age_days": prefs.max_age_days}


def create_app(*, config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    # Fail here rather than one confusing run at a time: a bad effort level
    # would otherwise surface as every call in that phase dying.
    agent_module.check(cfg)
    app = FastAPI(title="jobhunt", docs_url=None, redoc_url=None)
    app.state.jh = AppState(cfg)

    @app.middleware("http")
    async def _mark_activity(request: Any, call_next: Any) -> Any:
        # Every request counts, including the poll behind an open tab: a page
        # someone is looking at is a server someone is using.
        app.state.jh.idle.touch()
        return await call_next(request)

    # --- filters ------------------------------------------------------

    @app.get("/api/filters")
    def read_filters() -> dict[str, Any]:
        return {
            "filters": _filters_payload(cfg),
            "vocab": vocab_module.build(cfg),
        }

    @app.post("/api/filters")
    def write_filters(payload: dict[str, Any]) -> Any:
        prefs, _ = prefs_module.load(cfg)
        try:
            updated = webfilters.apply(prefs, payload)
        except webfilters.FieldError as exc:
            return JSONResponse(
                status_code=422, content={"field": exc.field, "message": str(exc)}
            )
        prefs_module.save(cfg, updated)
        matched, total = prefs_module.title_impact(cfg, updated)
        return {
            "filters": _filters_payload(cfg),
            "title_impact": {"matched": matched, "total": total},
        }

    # --- saved title groups -------------------------------------------

    @app.post("/api/title-groups")
    def save_title_group(payload: dict[str, Any]) -> Any:
        """Name the titles the form currently holds.

        Deliberately not `/api/filters`: naming a set worth returning to should
        never change what the next run searches for.
        """
        prefs, _ = prefs_module.load(cfg)
        try:
            updated = prefs_module.set_group(
                prefs, str(payload.get("name") or ""), list(payload.get("titles") or [])
            )
        except prefs_module.PreferenceError as exc:
            # Tagged against the titles field, because that is the input the
            # group was built from and the only one the form can mark.
            return JSONResponse(status_code=422, content={"field": "titles", "message": str(exc)})
        prefs_module.save(cfg, updated)
        return {"title_groups": updated.title_groups}

    @app.delete("/api/title-groups/{name}")
    def delete_title_group(name: str) -> Any:
        prefs, _ = prefs_module.load(cfg)
        try:
            updated = prefs_module.delete_group(prefs, name)
        except prefs_module.PreferenceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        prefs_module.save(cfg, updated)
        return {"title_groups": updated.title_groups}

    # --- applied ------------------------------------------------------

    @app.get("/api/applied")
    def read_applied() -> dict[str, Any]:
        return {"applied": applied_module.applied_job_ids(cfg)}

    @app.post("/api/applied/{job_id}")
    def set_applied(job_id: int, payload: dict[str, Any]) -> Any:
        """Record that an application went out, or take that back.

        The only action that removes a job from the shortlist for good, so it
        toggles rather than committing one way.
        """
        wanted = payload.get("applied")
        if not isinstance(wanted, bool):
            return JSONResponse(
                status_code=422,
                content={"message": "'applied' must be true or false"},
            )
        try:
            state = applied_module.set_applied(cfg, job_id, wanted)
        except applied_module.UnknownJob as exc:
            return JSONResponse(
                status_code=422, content={"message": f"no job {exc.job_id}"}
            )
        return {"job_id": job_id, "applied": state}

    # --- outreach -------------------------------------------------------

    # Routes plus the queue that releases a DM once its invite lands. The
    # provider is a stub, so nothing here reaches LinkedIn.
    outreach_sender = outreach_stub.StubProvider(cfg)
    app.state.outreach_sender = outreach_sender
    outreach_routes.register(app, cfg, outreach_sender)
    # The event is kept so the thread can be stopped: a `create_app` per test, or
    # per reload, would otherwise leave a daemon thread polling behind it.
    app.state.outreach_stop = threading.Event()
    app.state.outreach_thread = outreach_poller.watch(
        cfg, outreach_sender, stop=app.state.outreach_stop
    )
    app.router.on_shutdown.append(app.state.outreach_stop.set)

    # --- feeds --------------------------------------------------------

    def _feeds_payload() -> dict[str, Any]:
        prefs, _ = prefs_module.load(cfg)
        proposals = categories_module.propose(cfg, prefs)
        with session_scope(cfg.db_path) as session:
            approved = session.execute(
                select(
                    Board.provider, Board.token, Board.status,
                    Board.last_job_count, Board.last_fetched_at,
                ).where(
                    Board.discovered_via == categories_module.DISCOVERED_VIA,
                    # Deliberately not filtered on status: this feature ships
                    # no prober, and the promise that replaces one is that the
                    # panel reports what happened to every approved feed. A
                    # guess that died has to stay visible reading "did not
                    # resolve", or the user re-approves it forever. Retired
                    # feeds drop out by note instead, since retiring is a
                    # decision rather than a failure.
                    Board.notes == categories_module.APPROVED_NOTE,
                )
            ).all()
        return {
            # `propose` returns nothing for an empty title list whatever is on
            # disk, so the panel needs to know which of the two empty states it
            # is looking at before it can tell the user what to do about it.
            "has_titles": bool(prefs.titles),
            "proposals": [dataclasses.asdict(p) for p in proposals if not p.registered],
            "approved": [
                {
                    "provider": provider, "token": token, "status": status,
                    "last_job_count": last_job_count,
                    "last_fetched_at": last_fetched_at.isoformat() if last_fetched_at else None,
                }
                for provider, token, status, last_job_count, last_fetched_at in approved
            ],
        }

    @app.get("/api/feeds")
    def read_feeds() -> dict[str, Any]:
        return _feeds_payload()

    @app.post("/api/feeds")
    def write_feeds(payload: dict[str, Any]) -> Any:
        def pairs(key: str) -> list[tuple[str, str]]:
            rows = payload.get(key) or []
            if not isinstance(rows, list):
                raise _BadFeedRequest(f"{key!r} must be a list")
            out = []
            for row in rows:
                if not isinstance(row, dict):
                    raise _BadFeedRequest(f"each entry in {key!r} must be an object")
                provider, token = row.get("provider"), row.get("token")
                if not provider:
                    raise _BadFeedRequest(f"each entry in {key!r} needs a provider")
                if not isinstance(provider, str):
                    raise _BadFeedRequest(f"each provider in {key!r} must be a string")
                if provider not in categories_module.VOCABULARY:
                    raise _BadFeedRequest(f"{provider} is not a source that can be narrowed")
                # A dict or list token binds straight into a SQL parameter
                # and raises deep in the driver, so the shape is refused here
                # where it can still become the 422 this class promises.
                if token is not None and not isinstance(token, str):
                    raise _BadFeedRequest(f"each token in {key!r} must be a string")
                if token:
                    out.append((provider, token))
            return out

        try:
            to_approve, to_retire = pairs("approve"), pairs("retire")
        except _BadFeedRequest as exc:
            return JSONResponse(status_code=422, content={"message": exc.message})
        categories_module.approve(cfg, to_approve)
        categories_module.retire(cfg, to_retire)
        return _feeds_payload()

    # --- runs ---------------------------------------------------------

    @app.post("/api/runs")
    def start_run() -> Any:
        jh: AppState = app.state.jh
        if jh.supervisor is not None and jh.supervisor.state.running:
            return JSONResponse(
                status_code=409,
                content={"started": False, "message": "a run is already going"},
            )
        # A run already paused is resumed rather than restarted, so pressing
        # Start after a pause does not throw away the boards already fetched.
        if jh.supervisor is not None and jh.supervisor.state.resumable:
            jh.supervisor.resume()
            return {"started": True, "resumed": True}

        pipeline = build_pipeline(cfg, jh)
        run_id = history_module.new_id()
        jh.supervisor = RunSupervisor(
            pipeline=pipeline,
            log=jh.log,
            store=lambda state: history_module.save(cfg, state.run_id, _run_payload(state, jh)),
            clock=lambda: utcnow().isoformat(),
            max_gate_rounds=int(
                cfg.get("ranking", "max_gate_rounds", default=DEFAULT_GATE_ROUNDS)
            ),
        )
        jh.supervisor.start(run_id=run_id)
        return {"started": True}

    @app.get("/api/fx")
    def rates() -> dict[str, Any]:
        """The snapshot a run would use right now, so the form can convert."""
        from jobhunt.web import fx as fx_module

        return fx_module.load(cfg).as_dict()

    @app.get("/api/runs/current")
    def current_run() -> dict[str, Any]:
        return app.state.jh.state_dict()

    @app.get("/api/runs")
    def run_history() -> dict[str, Any]:
        return {"runs": history_module.listing(cfg)}

    @app.get("/api/runs/{run_id}")
    def one_run(run_id: str) -> Any:
        body = history_module.load(cfg, run_id)
        if body is None:
            return JSONResponse(status_code=404, content={"message": f"no run {run_id} on file"})
        return body

    @app.post("/api/runs/current/pause")
    def pause_run() -> dict[str, Any]:
        jh: AppState = app.state.jh
        if jh.supervisor is None or not jh.supervisor.state.running:
            return {"paused": False, "reason": "nothing is running"}
        jh.supervisor.request_pause()
        return {"paused": True}

    @app.post("/api/runs/current/resume")
    def resume_run() -> Any:
        jh: AppState = app.state.jh
        if jh.supervisor is None or not jh.supervisor.state.resumable:
            return JSONResponse(
                status_code=409,
                content={"resumed": False, "message": "there is no paused run to resume"},
            )
        jh.supervisor.resume()
        return {"resumed": True}

    @app.post("/api/runs/current/stop")
    def stop_run() -> dict[str, Any]:
        jh: AppState = app.state.jh
        if jh.supervisor is None or not jh.supervisor.state.running:
            # Stopping nothing is not a failure. Say so plainly rather than
            # raising at someone who clicked one time too many.
            return {"stop_requested": False, "reason": "nothing is running"}
        jh.supervisor.request_stop()
        return {"stop_requested": True}

    @app.get("/api/runs/current/events")
    async def run_events(request: Request) -> StreamingResponse:
        jh: AppState = app.state.jh
        last_id = request.headers.get("last-event-id")
        cursor = int(last_id) if last_id and last_id.isdigit() else 0

        async def stream():
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                for event in jh.log.since(cursor):
                    cursor = event.seq
                    yield to_sse(event)
                running = jh.supervisor is not None and jh.supervisor.state.running
                if not running and cursor >= jh.log.latest_seq():
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


    # --- the master CV ------------------------------------------------

    @app.get("/api/profile")
    def read_profile() -> Any:
        try:
            master = profile_module.read(cfg)
        except profile_module.ProfileError as exc:
            return JSONResponse(status_code=404, content={"message": str(exc)})
        return {
            "path": str(master.path),
            "text": master.text,
            "modified_at": master.modified_at.isoformat(),
        }

    @app.post("/api/profile")
    def save_profile(payload: dict[str, Any]) -> Any:
        try:
            backup = profile_module.write(cfg, str(payload.get("text", "")))
        except profile_module.ProfileError as exc:
            return JSONResponse(status_code=422, content={"field": "text", "message": str(exc)})
        return {"saved": True, "backup": backup.name if backup else None}

    @app.get("/api/profile/backups")
    def list_backups() -> dict[str, Any]:
        return {
            "backups": [
                {"name": b.name, "taken_at": b.taken_at.isoformat(), "size": b.size}
                for b in profile_module.backups(cfg)
            ]
        }

    @app.post("/api/profile/restore")
    def restore_backup(payload: dict[str, Any]) -> Any:
        try:
            profile_module.restore(cfg, str(payload.get("name", "")))
        except profile_module.ProfileError as exc:
            return JSONResponse(status_code=422, content={"field": "name", "message": str(exc)})
        return {"restored": True}

    @app.post("/api/profile/compile")
    def compile_profile(payload: dict[str, Any]) -> dict[str, Any]:
        import base64

        result = profile_module.compile_tex(cfg, text=payload.get("text"))
        return {
            "ok": result.ok,
            "log": result.log,
            "pdf": base64.b64encode(result.pdf_bytes).decode() if result.ok else None,
        }

    # --- batch tailoring ----------------------------------------------

    @app.get("/api/tailor")
    def tailor_state() -> dict[str, Any]:
        jh: AppState = app.state.jh
        if jh.batch is None:
            return {"jobs": [], "running": False}
        return {"jobs": jh.batch.state(), "running": jh.batch.running}

    @app.post("/api/tailor")
    def start_tailoring(payload: dict[str, Any]) -> Any:
        jh: AppState = app.state.jh
        job_ids = [int(value) for value in (payload.get("job_ids") or [])]
        if not job_ids:
            return JSONResponse(
                status_code=422,
                content={"field": "job_ids", "message": "pick at least one job to tailor"},
            )
        if jh.batch is not None and jh.batch.running:
            return JSONResponse(
                status_code=409,
                content={"started": False, "message": "a tailoring batch is already going"},
            )
        jh.batch = tailor_module.TailorBatch(
            job_ids=job_ids, steps=tailor_module.ClaudeSteps(cfg), log=jh.log
        )
        _name_the_rows(cfg, jh.batch)
        jh.batch.start()
        return {"started": True, "jobs": jh.batch.state()}

    @app.post("/api/tailor/stop")
    def stop_tailoring() -> dict[str, Any]:
        jh: AppState = app.state.jh
        if jh.batch is None or not jh.batch.running:
            return {"stop_requested": False, "reason": "nothing is tailoring"}
        jh.batch.request_stop()
        return {"stop_requested": True}

    # --- the revision studio ------------------------------------------
    # Optional, and always after the fact: the batch has already shipped every
    # CV by the time any of this is reachable.

    def _refused(exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"field": "revise", "message": str(exc)})

    @app.get("/api/revise")
    def revisable() -> dict[str, Any]:
        """Every CV with a folder, which is every CV worth opening."""
        jh: AppState = app.state.jh
        rows = jh.batch.state() if jh.batch is not None else []
        open_now = {session.job_id: session for session in jh.desk.sessions()}
        return {
            "jobs": [
                row | {
                    "ahead": open_now[row["job_id"]].ahead if row["job_id"] in open_now else 0,
                    "opened": row["job_id"] in open_now,
                    # Three things have to be true, and existence is only one:
                    # the folder appears when tailoring starts, the CV is
                    # written gradually, and a later round overwrites it. A
                    # draft taken at any of those moments is unusable.
                    "ready": (
                        row.get("state") not in ("queued", "running")
                        and revise_module.has_cv(row.get("folder"))
                    ),
                }
                for row in rows
                if row.get("folder")
            ]
        }

    @app.post("/api/revise/{job_id}")
    def open_revision(job_id: int) -> Any:
        jh: AppState = app.state.jh
        row = _row_for(jh, job_id)
        if row is None or not row.get("folder"):
            return _refused(revise_module.ReviseError(f"job {job_id} has no tailored CV yet"))
        try:
            session = jh.desk.open(
                job_id,
                folder=str(row["folder"]),
                title=str(row.get("title") or ""),
                company=str(row.get("company") or ""),
                fit=row.get("fit"),
            )
        except revise_module.ReviseError as exc:
            return _refused(exc)
        return session.as_dict()

    @app.get("/api/revise/{job_id}")
    def revision_state(job_id: int) -> Any:
        session = app.state.jh.desk.get(job_id)
        if session is None:
            return _refused(revise_module.ReviseError(f"no revision open for job {job_id}"))
        return session.as_dict()

    @app.post("/api/revise/{job_id}/message")
    def send_revision(job_id: int, payload: dict[str, Any]) -> Any:
        try:
            session = app.state.jh.desk.send(job_id, str(payload.get("message", "")))
        except revise_module.ReviseError as exc:
            return _refused(exc)
        return session.as_dict()

    @app.get("/api/revise/{job_id}/preview")
    def revision_preview(job_id: int) -> Any:
        try:
            return app.state.jh.desk.preview(job_id)
        except revise_module.ReviseError as exc:
            return _refused(exc)

    @app.post("/api/revise/{job_id}/sync")
    def sync_revision(job_id: int) -> Any:
        try:
            session = app.state.jh.desk.sync(job_id)
        except revise_module.ReviseError as exc:
            return _refused(exc)
        return session.as_dict()

    @app.post("/api/revise/{job_id}/discard")
    def discard_revision(job_id: int) -> Any:
        try:
            session = app.state.jh.desk.discard(job_id)
        except revise_module.ReviseError as exc:
            return _refused(exc)
        return session.as_dict()

    # --- the built ui -------------------------------------------------

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/")
    def index() -> Any:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            return JSONResponse(
                status_code=503,
                content={"message": "the interface is not built yet. run: npm --prefix ui run build"},
            )
        return FileResponse(page)

    return app
