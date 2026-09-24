"""The CV builder over HTTP.

The form is a convenience; every rule is enforced here again. Refusals carry
the field path, so the form can put each message next to its input.
"""
from __future__ import annotations

import copy

import pytest
from conftest import FIXTURES, load_fixture, passing
from fastapi.testclient import TestClient

from jobhunt.cv import convert, importer, model, render, templates
from jobhunt.web import cv as cv_routes
from jobhunt.web.app import create_app


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

GOOD_TEMPLATE = (FIXTURES / "cv" / "upload_template.tex").read_bytes()


@pytest.fixture
def uploads(client, cfg):
    cfg.raw["tailoring"]["ats_check"] = str(cfg.db_path.parent / "no_ats.py")
    client.app.state.cv_upload = convert.UploadDesk(cfg, agent=lambda p: "", runner=passing, background=False)
    return client


def test_the_gallery_lists_both_built_ins_with_the_default_marked(client):
    body = client.get("/api/cv/templates").json()

    assert body["default_id"] == "classic"
    rows = {row["id"]: row for row in body["templates"]}
    assert rows["classic"]["default"] and rows["classic"]["builtin"] and not rows["modern"]["default"]


def test_a_pdf_upload_is_refused_with_the_reason(uploads):
    response = uploads.post("/api/cv/templates", files={"file": ("cv.pdf", b"%PDF-1.7", "application/pdf")})

    assert response.status_code == 422 and "only .tex" in response.json()["message"]


def test_a_template_upload_is_checked_and_can_be_accepted(uploads):
    started = uploads.post(
        "/api/cv/templates", files={"file": ("mine.tex", GOOD_TEMPLATE, "application/x-tex")}
    ).json()
    assert started["state"] == "done" and started["acceptable"], started["problems"]
    assert uploads.get("/api/cv/templates/upload/preview.pdf").content == b"%PDF-1.7 fake"

    added = uploads.post("/api/cv/templates/upload/accept", json={"name": "Mine"}).json()

    assert added["name"] == "Mine" and not added["builtin"]
    assert added["id"] in [row["id"] for row in uploads.get("/api/cv/templates").json()["templates"]]


def test_choosing_a_default_empties_the_master(client):
    client.put("/api/cv/profile", json={"profile": fixture()})
    client.post("/api/cv/master")

    status = client.post("/api/cv/templates/modern/default").json()

    assert status["state"] == "empty" and "Modern" in status["reason"]
    assert client.get("/api/cv/templates").json()["default_id"] == "modern"


def test_an_unknown_template_is_a_404(client):
    assert client.post("/api/cv/templates/nope/default").status_code == 404
    assert client.get("/api/cv/templates/nope/source.tex").status_code == 404


def test_built_ins_cannot_be_renamed_or_deleted(client):
    assert client.patch("/api/cv/templates/classic", json={"name": "Mine"}).status_code == 409
    assert client.delete("/api/cv/templates/classic").status_code == 409


def test_an_upload_can_be_renamed_and_removed(uploads, cfg):
    added = templates.add(cfg, "Mine", GOOD_TEMPLATE.decode(), engine="lualatex")

    assert uploads.patch(f"/api/cv/templates/{added.id}", json={"name": "Yours"}).json()["name"] == "Yours"
    assert uploads.delete(f"/api/cv/templates/{added.id}").status_code == 200
    assert added.id not in [row["id"] for row in uploads.get("/api/cv/templates").json()["templates"]]


def test_a_template_source_downloads_as_tex(client):
    response = client.get("/api/cv/templates/classic/source.tex")

    assert response.status_code == 200
    assert 'filename="Classic-template.tex"' in response.headers["content-disposition"]
    assert b"\\resumeSubheading" in response.content


def test_a_preview_is_served_as_a_pdf(client):
    response = client.get("/api/cv/templates/modern/preview.pdf")

    assert response.status_code == 200 and response.content == b"%PDF-1.7 fake"


def test_the_contract_is_readable(client):
    assert b"hidable" in client.get("/api/cv/contract.md").content


def test_a_template_with_an_accented_name_downloads_under_a_readable_name(client, cfg):
    added = templates.add(cfg, "Özge look", GOOD_TEMPLATE.decode(), engine="lualatex")

    response = client.get(f"/api/cv/templates/{added.id}/source.tex")

    assert 'filename="Ozge_look-template.tex"' in response.headers["content-disposition"]
