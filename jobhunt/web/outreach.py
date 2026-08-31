"""Outreach over HTTP. Thin on purpose - every decision is in jobhunt/outreach.

Refusals matter as much as successes here: the drawer disables a button and
states why, which it can only do if the reason survives the trip.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from jobhunt.config import Config
from jobhunt.db.models import Contact, Job, Outreach
from jobhunt.db.session import session_scope
from jobhunt.outreach import caps, discovery, provider, service
from jobhunt.outreach.unipile_client import UnipileError

# Ordering for "most advanced state" across a job's contacts. `failed` and
# `cancelled` are not progress - a job whose only contact bounced should not
# outrank one that is merely drafted, so both sit below "drafted".
_STATE_RANK = {
    "none": 0,
    "failed": 1,
    "cancelled": 1,
    "drafted": 2,
    "queued": 3,
    "sent": 4,
}

# What `discovery` can produce, plus the box in the drawer. Anything else is a
# typo or a caller inventing provenance, and stored provenance the ranker and
# the drawer both read is not a field to take on trust.
CONTACT_ORIGINS = ("job_poster", "company_search", "manual")


def _existing_profile_urls(session) -> set[str]:
    # Normalized on read, not trusted from storage: `add_contact` normalizes
    # before writing today, but this set is what decides "existing" for every
    # future writer too, and a raw URL here would silently stop matching a
    # candidate's normalized one the day that stops being the only writer.
    return {
        normalized
        for url, in session.execute(select(Contact.profile_url))
        if (normalized := service.normalize_profile_url(url))
    }


def _candidate_payload(
    candidates: list[discovery.ContactCandidate], existing_urls: set[str]
) -> list[dict[str, Any]]:
    payload = []
    for candidate in candidates:
        normalized = service.normalize_profile_url(candidate.profile_url)
        payload.append(
            {
                "full_name": candidate.full_name,
                "headline": candidate.headline,
                "profile_url": candidate.profile_url,
                "origin": candidate.origin,
                "existing": normalized is not None and normalized in existing_urls,
            }
        )
    return payload


def register(app: FastAPI, config: Config, sender: provider.LinkedInProvider) -> None:
    """Attach the outreach routes to an app that already exists."""

    def guarded(call, *args, **kwargs) -> Any:
        try:
            return call(*args, **kwargs)
        except service.UnknownJob as error:
            raise HTTPException(status_code=404, detail=f"No job {error.job_id}.") from error
        except service.UnknownContact as error:
            raise HTTPException(
                status_code=404, detail=f"No contact {error.contact_id} on this job."
            ) from error
        except caps.CapReached as error:
            raise HTTPException(status_code=409, detail=error.message) from error
        except service.IllegalTransition as error:
            raise HTTPException(
                status_code=409, detail=f"Cannot go from {error.current} to {error.target}."
            ) from error
        except service.TooLong as error:
            raise HTTPException(
                status_code=422,
                detail=f"{error.length} characters; this route allows {error.limit}.",
            ) from error

    @app.get("/api/outreach/budget")
    def read_budget() -> dict[str, Any]:
        with session_scope(config.db_path) as session:
            return service._budget_payload(config, session)

    @app.get("/api/outreach/states")
    def read_states() -> dict[str, dict[int, str]]:
        # One query over every outreach row rather than a per-job loop through
        # the service: the shortlist needs a status dot for every row on the
        # page at once, and that has to stay cheap as the shortlist grows.
        with session_scope(config.db_path) as session:
            rows = session.execute(select(Outreach.job_id, Outreach.state)).all()
        best: dict[int, str] = {}
        for job_id, state in rows:
            current = best.get(job_id)
            if current is None or _STATE_RANK[state] > _STATE_RANK[current]:
                best[job_id] = state
        return {"states": best}

    @app.get("/api/outreach/{job_id}")
    def read_job(job_id: int) -> dict[str, Any]:
        return guarded(service.for_job, config, job_id, sender)

    @app.post("/api/outreach/{job_id}/find")
    def find(job_id: int) -> dict[str, Any]:
        """Both kinds of candidate, stated ones first.

        Reads `app.state.outreach_sender` rather than the closed-over `sender`
        so a test (or a future admin toggle) can swap the sender after the app
        is built, the same seam `outreach_stop` already uses.

        The company search fires only here, and only when the current sender
        actually exposes one - the stub, the default, does not. Nothing on the
        fetch/sync path holds a reference to this function at all.
        """
        current_sender = getattr(app.state, "outreach_sender", sender)
        with session_scope(config.db_path) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"No job {job_id}.")
            candidates = discovery.find_contacts(job)
            client = getattr(current_sender, "client", None)
            company = job.company.name if job.company else None
            if client is not None and hasattr(client, "search_people") and company:
                try:
                    candidates = candidates + discovery.search_company(
                        client, company, config=config
                    )
                except UnipileError as error:
                    existing_urls = _existing_profile_urls(session)
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": str(error),
                            "candidates": _candidate_payload(candidates, existing_urls),
                        },
                    ) from error
            existing_urls = _existing_profile_urls(session)
            return {"candidates": _candidate_payload(candidates, existing_urls)}

    @app.post("/api/outreach/{job_id}/contacts")
    def add_contact(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        name = (payload.get("full_name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="A contact needs a name.")
        # Where a contact came from is the whole point of `find`: a person the
        # posting itself named is worth more than one a company search inferred,
        # and both are worth more than a name typed into the box. Adding a found
        # candidate used to record it as manual, erasing that difference.
        origin = payload.get("origin") or "manual"
        if origin not in CONTACT_ORIGINS:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown origin {origin!r}. Use one of: {', '.join(CONTACT_ORIGINS)}.",
            )
        return guarded(
            service.add_contact,
            config,
            job_id,
            full_name=name,
            profile_url=(payload.get("profile_url") or None),
            headline=(payload.get("headline") or None),
            origin=origin,
        )

    @app.delete("/api/outreach/contacts/{contact_id}")
    def drop_contact(contact_id: int) -> dict[str, bool]:
        guarded(service.remove_contact, config, contact_id)
        return {"removed": True}

    @app.patch("/api/outreach/contacts/{contact_id}/status")
    def set_status(contact_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        # Stub-only. The real provider turns this into a read.
        return guarded(
            service.set_status,
            config,
            contact_id,
            is_connection=payload.get("is_connection"),
            can_send_inmail=payload.get("can_send_inmail"),
        )

    @app.post("/api/outreach/{job_id}/{contact_id}/draft")
    def draft(job_id: int, contact_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return guarded(
            service.draft, config, job_id, contact_id, sender, route=(payload or {}).get("route")
        )

    @app.put("/api/outreach/{job_id}/{contact_id}/body")
    def save_body(job_id: int, contact_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return guarded(
            service.save_body,
            config,
            job_id,
            contact_id,
            payload.get("body") or "",
            route=payload.get("route"),
        )

    @app.post("/api/outreach/{job_id}/{contact_id}/approve")
    def approve(job_id: int, contact_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return guarded(
            service.approve, config, job_id, contact_id, sender, route=(payload or {}).get("route")
        )

    @app.post("/api/outreach/{job_id}/{contact_id}/cancel")
    def cancel(job_id: int, contact_id: int) -> dict[str, Any]:
        return guarded(service.cancel, config, job_id, contact_id)
