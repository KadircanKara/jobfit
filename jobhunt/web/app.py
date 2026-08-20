"""The HTTP surface.

Loopback only, single user, no auth. The browser enforces the same rules this
module does, but the browser is a convenience: a broken filter set that reached
the scraper would waste a whole run, so every rule is checked again here.
"""
from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from jobhunt import preferences as prefs_module
from jobhunt.config import Config
from jobhunt.config import load as load_config
from jobhunt.web import filters as webfilters
from jobhunt.web import profile as profile_module
from jobhunt.web import tailor as tailor_module
from jobhunt.web import vocab as vocab_module
from jobhunt.web.events import EventLog, to_sse
from jobhunt.web.runs import RunSupervisor

STATIC_DIR = pathlib.Path(__file__).parent / "static"


class AppState:
    """What the process holds between requests: one run, one event log."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.log = EventLog()
        self.supervisor: RunSupervisor | None = None
        self.batch: tailor_module.TailorBatch | None = None

    def state_dict(self) -> dict[str, Any]:
        if self.supervisor is None:
            return {
                "phase": "idle", "running": False, "outcome": None, "error": None,
                "degraded": [], "counters": {}, "results": [], "last_seq": self.log.latest_seq(),
            }
        state = self.supervisor.state
        return {
            "phase": state.phase,
            "running": state.running,
            "outcome": state.outcome,
            "error": state.error,
            "degraded": list(state.degraded),
            "counters": dict(state.counters),
            "results": list(state.results),
            "stopping": self.supervisor.stopping,
            "last_seq": self.log.latest_seq(),
        }


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

    pipeline.on_board = on_board
    pipeline.should_stop = lambda: bool(state.supervisor and state.supervisor.stopping)
    return pipeline


def _filters_payload(config: Config) -> dict[str, Any]:
    prefs, _ = prefs_module.load(config)
    return dataclasses.asdict(prefs) | {"max_age_days": prefs.max_age_days}


def create_app(*, config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(title="jobhunt", docs_url=None, redoc_url=None)
    app.state.jh = AppState(cfg)

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

    # --- runs ---------------------------------------------------------

    @app.post("/api/runs")
    def start_run() -> Any:
        jh: AppState = app.state.jh
        if jh.supervisor is not None and jh.supervisor.state.running:
            return JSONResponse(
                status_code=409,
                content={"started": False, "message": "a run is already going"},
            )
        pipeline = build_pipeline(cfg, jh)
        jh.supervisor = RunSupervisor(pipeline=pipeline, log=jh.log)
        jh.supervisor.start()
        return {"started": True}

    @app.get("/api/fx")
    def rates() -> dict[str, Any]:
        """The snapshot a run would use right now, so the form can convert."""
        from jobhunt.web import fx as fx_module

        return fx_module.load(cfg).as_dict()

    @app.get("/api/runs/current")
    def current_run() -> dict[str, Any]:
        return app.state.jh.state_dict()

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
        jh.batch.start()
        return {"started": True, "jobs": jh.batch.state()}

    @app.post("/api/tailor/stop")
    def stop_tailoring() -> dict[str, Any]:
        jh: AppState = app.state.jh
        if jh.batch is None or not jh.batch.running:
            return {"stop_requested": False, "reason": "nothing is tailoring"}
        jh.batch.request_stop()
        return {"stop_requested": True}

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
