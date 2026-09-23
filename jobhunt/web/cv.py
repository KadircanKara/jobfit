"""The CV builder over HTTP: the profile, the master CV, and the one-time import.

Loopback only, like the rest of the app. Every rule the form enforces is
enforced again here, and a refused profile comes back with every problem and
its field path, so the form can mark each input rather than one banner.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response

from jobhunt.config import Config
from jobhunt.cv import importer, master, model
from jobhunt.cv import store as cvstore


def register(app: FastAPI, config: Config) -> None:
    """Attach the CV routes to an app that already exists."""
    # On app.state, so a test can swap in a fake LaTeX runner or agent without
    # the routes knowing.
    app.state.cv_runner = None
    app.state.cv_import = importer.ImportDesk(config)

    @app.get("/api/cv/profile")
    def read_profile() -> Any:
        try:
            saved = cvstore.read(config)
        except cvstore.StoreError as exc:
            return _refused("profile", str(exc))
        master_exists = cvstore.master_path(config).exists()
        return {
            "profile": saved.profile.model_dump(mode="json") if saved else None,
            "path": str(cvstore.profile_path(config)),
            "saved_at": saved.saved_at.isoformat() if saved else None,
            "master_exists": master_exists,
            "import_available": master_exists and saved is None,
        }

    @app.put("/api/cv/profile")
    def save_profile(payload: dict[str, Any]) -> Any:
        try:
            profile = model.parse(payload.get("profile"))
        except model.ProfileInvalid as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "field": exc.field,
                    "message": exc.message,
                    "problems": [{"field": field, "message": message} for field, message in exc.problems],
                },
            )
        backup = cvstore.write(config, profile)
        return {"saved": True, "backup": backup.name if backup else None, "master": _status(config)}

    @app.get("/api/cv/profile/backups")
    def list_backups() -> dict[str, Any]:
        return {
            "backups": [
                {"name": b.name, "taken_at": b.taken_at.isoformat(), "size": b.size}
                for b in cvstore.backups(config)
            ]
        }

    @app.post("/api/cv/profile/restore")
    def restore_backup(payload: dict[str, Any]) -> Any:
        try:
            cvstore.restore(config, str(payload.get("name", "")))
        except cvstore.StoreError as exc:
            return _refused("name", str(exc))
        return {"restored": True, "master": _status(config)}

    @app.get("/api/cv/master")
    def master_status() -> Any:
        try:
            return master.status(config).as_dict()
        except cvstore.StoreError as exc:
            return _refused("profile", str(exc))

    @app.post("/api/cv/master")
    def generate_master() -> Any:
        try:
            outcome = master.generate(config, runner=app.state.cv_runner)
        except master.MasterError as exc:
            return _conflict(str(exc))
        except cvstore.StoreError as exc:
            return _refused("profile", str(exc))
        return {
            "ok": outcome.ok,
            "log": outcome.log,
            "pages": outcome.pages,
            "master": outcome.status.as_dict(),
        }

    @app.get("/api/cv/master.pdf")
    def master_pdf(download: bool = False) -> Any:
        return _file(config, "pdf", "application/pdf", "attachment" if download else "inline")

    @app.get("/api/cv/master.tex")
    def master_tex() -> Any:
        return _file(config, "tex", "application/x-tex", "attachment")

    # --- the one-time import ----------------------------------------------

    @app.get("/api/cv/import")
    def import_state() -> dict[str, Any]:
        return app.state.cv_import.snapshot()

    @app.post("/api/cv/import")
    def start_import() -> Any:
        try:
            return app.state.cv_import.start()
        except importer.ImportFailed as exc:
            return _conflict(str(exc))

    @app.get("/api/cv/import/preview.pdf")
    def import_preview() -> Any:
        pdf = app.state.cv_import.pdf
        if not pdf:
            return JSONResponse(status_code=404, content={"message": "there is no preview yet"})
        return Response(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @app.post("/api/cv/import/accept")
    def accept_import() -> Any:
        try:
            backup = app.state.cv_import.accept()
        except importer.ImportFailed as exc:
            return _conflict(str(exc))
        return {"saved": True, "backup": backup.name if backup else None}

    @app.post("/api/cv/import/discard")
    def discard_import() -> Any:
        try:
            app.state.cv_import.discard()
        except importer.ImportFailed as exc:
            return _conflict(str(exc))
        return app.state.cv_import.snapshot()


def download_name(person: str, kind: str) -> str:
    """`Kadircan_Kara-CV.pdf`: the name the tailoring skill gives every deliverable."""
    ascii_name = unicodedata.normalize("NFKD", person).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", ascii_name)
    return f"{'_'.join(words) or 'Master'}-CV.{kind}"


def _file(config: Config, kind: str, media_type: str, disposition: str) -> Any:
    try:
        path = master.download(config, kind)
        saved = cvstore.read(config)
    except master.MasterError as exc:
        return _conflict(str(exc))
    except cvstore.StoreError as exc:
        return _refused("profile", str(exc))
    return FileResponse(
        path,
        media_type=media_type,
        filename=download_name(saved.profile.basics.name if saved else "", kind),
        content_disposition_type=disposition,
        headers={"Cache-Control": "no-store"},
    )


def _status(config: Config) -> dict[str, Any]:
    return master.status(config).as_dict()


def _refused(field: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"field": field, "message": message})


def _conflict(message: str) -> JSONResponse:
    return JSONResponse(status_code=409, content={"message": message})
