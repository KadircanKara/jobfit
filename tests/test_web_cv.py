"""The CV builder over HTTP.

The form is a convenience; every rule is enforced here again. Refusals carry
the field path, so the form can put each message next to its input.
"""
from __future__ import annotations

import copy

import pytest
from conftest import load_fixture
from fastapi.testclient import TestClient

from jobhunt.cv import importer, model, render, templates
from jobhunt.web import cv as cv_routes
from jobhunt.web.app import create_app


def passing(argv, cwd):
    (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
    return 0, "Output written on cv.pdf (1 page, 13 bytes)."


@pytest.fixture
def cv_source(tmp_path, cfg):
    folder = tmp_path / "CV_Source"
    folder.mkdir()
    cfg.raw.setdefault("tailoring", {})["master_tex"] = str(folder / "master.tex")
    return folder


@pytest.fixture
def client(cfg, cv_source):
    app = create_app(config=cfg)
    app.state.cv_runner = passing
    return TestClient(app)


def fixture() -> dict:
    return copy.deepcopy(load_fixture("cv/profile.json"))


def test_before_anything_there_is_no_profile(client):
    body = client.get("/api/cv/profile").json()

    assert body["profile"] is None and body["import_available"] is False


def test_an_import_is_offered_when_there_is_a_master_but_no_profile(client, cv_source):
    (cv_source / "master.tex").write_text("\\begin{document}\\end{document}", encoding="utf-8")

    assert client.get("/api/cv/profile").json()["import_available"] is True


def test_an_invalid_profile_is_refused_with_every_field_named(client):
    data = fixture()
    data["basics"]["name"] = ""
    data["experience"][0]["bullets"][0]["text"] = ""

    response = client.put("/api/cv/profile", json={"profile": data})

    assert response.status_code == 422
    body = response.json()
    assert body["field"] == "basics.name"
    assert [p["field"] for p in body["problems"]] == ["basics.name", "experience.0.bullets.0.text"]


def test_a_saved_profile_comes_back_and_the_master_is_still_empty(client):
    saved = client.put("/api/cv/profile", json={"profile": fixture()}).json()

    assert saved["saved"] and saved["master"]["state"] == "empty"
    assert client.get("/api/cv/profile").json()["profile"]["basics"]["name"] == "Ada Lovelace"


def test_backups_list_and_restore(client):
    client.put("/api/cv/profile", json={"profile": fixture()})
    renamed = fixture()
    renamed["basics"]["name"] = "Ada King"
    client.put("/api/cv/profile", json={"profile": renamed})

    backups = client.get("/api/cv/profile/backups").json()["backups"]
    assert len(backups) == 1
    assert client.post("/api/cv/profile/restore", json={"name": backups[0]["name"]}).status_code == 200
    assert client.get("/api/cv/profile").json()["profile"]["basics"]["name"] == "Ada Lovelace"


def test_restoring_a_name_that_is_not_a_backup_is_refused(client):
    assert client.post("/api/cv/profile/restore", json={"name": "../x"}).status_code == 422


def test_generating_without_a_profile_is_a_conflict(client):
    assert client.post("/api/cv/master").status_code == 409


def test_generate_then_download_both_files(client, cv_source):
    client.put("/api/cv/profile", json={"profile": fixture()})

    generated = client.post("/api/cv/master").json()
    assert generated["ok"] and generated["master"]["state"] == "ready"
    assert generated["missing"] == []

    inline = client.get("/api/cv/master.pdf")
    assert inline.status_code == 200 and inline.content == b"%PDF-1.7 fake"
    assert inline.headers["content-disposition"].startswith("inline")

    pdf = client.get("/api/cv/master.pdf?download=1")
    assert 'attachment; filename="Ada_Lovelace-CV.pdf"' == pdf.headers["content-disposition"]

    tex = client.get("/api/cv/master.tex")
    assert 'filename="Ada_Lovelace-CV.tex"' in tex.headers["content-disposition"]
    assert tex.content == (cv_source / "master.tex").read_bytes()


def test_downloads_are_a_conflict_while_empty(client):
    client.put("/api/cv/profile", json={"profile": fixture()})

    response = client.get("/api/cv/master.pdf?download=1")

    assert response.status_code == 409
    assert "generated" in response.json()["message"]
    assert client.get("/api/cv/master.tex").status_code == 409


def test_the_status_route_reports_the_state(client):
    client.put("/api/cv/profile", json={"profile": fixture()})
    client.post("/api/cv/master")
    changed = fixture()
    changed["basics"]["headline"] = "Engineer"
    client.put("/api/cv/profile", json={"profile": changed})

    assert client.get("/api/cv/master").json()["state"] == "stale"


def test_the_import_flow_end_to_end(client, cfg, cv_source):
    profile = model.parse(fixture())
    (cv_source / "master.tex").write_text(
        render.render(profile, templates.get(cfg, "classic").text()), encoding="utf-8"
    )
    client.app.state.cv_import = importer.ImportDesk(
        cfg, agent=lambda prompt: profile.model_dump_json(), runner=passing, background=False
    )

    started = client.post("/api/cv/import").json()
    assert started["state"] == "done" and started["report"]["faithful"]
    assert client.get("/api/cv/import/preview.pdf").content == b"%PDF-1.7 fake"

    assert client.post("/api/cv/import/accept").json()["saved"]
    assert client.get("/api/cv/profile").json()["profile"]["basics"]["name"] == "Ada Lovelace"
    assert client.post("/api/cv/import").status_code == 409


def test_a_download_name_is_the_person_in_ascii():
    assert cv_routes.download_name("Kadircan Kara", "pdf") == "Kadircan_Kara-CV.pdf"
    assert cv_routes.download_name("Özge Şahin", "tex") == "Ozge_Sahin-CV.tex"
    assert cv_routes.download_name("", "pdf") == "Master-CV.pdf"
