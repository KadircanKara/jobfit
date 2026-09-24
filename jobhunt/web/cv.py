"""The CV builder over HTTP: the profile, the master CV, the template library,
and the one-time import of master.tex.

Loopback only, like the rest of the app. Every rule the form enforces is
enforced again here, and a refused profile comes back with every problem and
its field path, so the form can mark each input rather than one banner.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from jobhunt.config import Config
from jobhunt.cv import convert, importer, master, model, preview, templates
from jobhunt.cv import store as cvstore


def register(app: FastAPI, config: Config) -> None:
    """Attach the CV routes to an app that already exists."""
    # On app.state, so a test can swap in a fake LaTeX runner or agent without
    # the routes knowing.
    app.state.cv_runner = None
    app.state.cv_import = importer.ImportDesk(config)
    app.state.cv_upload = convert.UploadDesk(config)

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
            "missing": list(outcome.missing),
            "master": outcome.status.as_dict(),
        }

    @app.get("/api/cv/master.pdf")
    def master_pdf(download: bool = False) -> Any:
        return _file(config, "pdf", "application/pdf", "attachment" if download else "inline")

    @app.get("/api/cv/master.tex")
    def master_tex() -> Any:
        return _file(config, "tex", "application/x-tex", "attachment")

    # --- templates ------------------------------------------------------------

    @app.get("/api/cv/templates")
    def list_templates() -> dict[str, Any]:
        current = templates.default_id(config)
        return {
            "default_id": current,
            "templates": [_row(template, current) for template in templates.all_templates(config)],
        }

    @app.post("/api/cv/templates")
    async def upload_template(file: UploadFile) -> Any:
        data = await file.read(convert.UPLOAD_LIMIT + 1)
        try:
            return app.state.cv_upload.start(file.filename or "", data)
        except convert.UploadRefused as exc:
            return _refused("file", str(exc))
        except convert.UploadFailed as exc:
            return _conflict(str(exc))

    @app.get("/api/cv/templates/upload")
    def upload_state() -> dict[str, Any]:
        return app.state.cv_upload.snapshot()

    @app.get("/api/cv/templates/upload/preview.pdf")
    def upload_preview() -> Any:
        return _desk_pdf(app.state.cv_upload.pdf)

    @app.post("/api/cv/templates/upload/accept")
    def accept_upload(payload: dict[str, Any]) -> Any:
        try:
            added = app.state.cv_upload.accept(str(payload.get("name", "")))
        except convert.UploadFailed as exc:
            return _conflict(str(exc))
        except templates.TemplateError as exc:
            return _refused("name", str(exc))
        return _row(added, templates.default_id(config))

    @app.post("/api/cv/templates/upload/discard")
    def discard_upload() -> Any:
        try:
            app.state.cv_upload.discard()
        except convert.UploadFailed as exc:
            return _conflict(str(exc))
        return app.state.cv_upload.snapshot()

    @app.patch("/api/cv/templates/{template_id}")
    def rename_template(template_id: str, payload: dict[str, Any]) -> Any:
        def renamed() -> Any:
            template = templates.rename(config, template_id, str(payload.get("name", "")))
            return _row(template, templates.default_id(config))

        return _template_call(renamed)

    @app.delete("/api/cv/templates/{template_id}")
    def remove_template(template_id: str) -> Any:
        return _template_call(lambda: templates.remove(config, template_id) or {"removed": True})

    @app.post("/api/cv/templates/{template_id}/default")
    def choose_default(template_id: str) -> Any:
        return _template_call(lambda: master.use_template(config, template_id).as_dict())

    @app.get("/api/cv/templates/{template_id}/source.tex")
    def template_source(template_id: str) -> Any:
        def serve() -> Any:
            template = templates.get(config, template_id)
            stem = ascii_stem(template.name) or "template"
            return FileResponse(
                template.folder / templates.SOURCE_NAME,
                media_type="application/x-tex",
                filename=f"{stem}-template.tex",
            )

        return _template_call(serve)

    @app.get("/api/cv/templates/{template_id}/thumbnail.png")
    def template_thumbnail(template_id: str) -> Any:
        def serve() -> Any:
            shown = preview.preview(config, template_id, runner=app.state.cv_runner)
            if not shown.ok or shown.png is None:
                return JSONResponse(status_code=404, content={"message": shown.log or "no thumbnail"})
            return Response(shown.png, media_type="image/png", headers={"Cache-Control": "no-cache"})

        return _template_call(serve)

    @app.get("/api/cv/templates/{template_id}/preview.pdf")
    def template_preview(template_id: str) -> Any:
        def serve() -> Any:
            shown = preview.preview(config, template_id, runner=app.state.cv_runner)
            if not shown.ok:
                return JSONResponse(status_code=422, content={"field": "template", "message": shown.log})
            return Response(shown.pdf, media_type="application/pdf", headers={"Cache-Control": "no-cache"})

        return _template_call(serve)

    @app.get("/api/cv/contract.md")
    def template_contract() -> Any:
        return FileResponse(templates.CONTRACT_PATH, media_type="text/markdown; charset=utf-8")

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
        return _desk_pdf(app.state.cv_import.pdf)

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


def _row(template: templates.Template, default_id: str) -> dict[str, Any]:
    return {
        "id": template.id,
        "name": template.name,
        "source": template.source,
        "engine": template.engine,
        "description": template.description,
        "builtin": template.builtin,
        "default": template.id == default_id,
    }


def _template_call(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except templates.UnknownTemplate as exc:
        return JSONResponse(status_code=404, content={"message": str(exc)})
    except templates.TemplateError as exc:
        return _conflict(str(exc))
    except cvstore.StoreError as exc:
        return _refused("profile", str(exc))


def download_name(person: str, kind: str) -> str:
    """`Kadircan_Kara-CV.pdf`: the name the tailoring skill gives every deliverable."""
    return f"{ascii_stem(person) or 'Master'}-CV.{kind}"


def ascii_stem(text: str) -> str:
    """A file-name-safe stem: accents folded, every other character a separator."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "_".join(re.findall(r"[A-Za-z0-9]+", folded))


def _desk_pdf(pdf: bytes) -> Any:
    if not pdf:
        return JSONResponse(status_code=404, content={"message": "there is no preview yet"})
    return Response(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})


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
