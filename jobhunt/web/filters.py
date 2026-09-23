"""Filter payloads from the browser, translated into Preferences.

The CLI already owns the hard rules: a location that cannot be placed, a
ceiling below the floor, an unknown work model. This module does not repeat
them. It turns typed JSON into the `key=value` strings `apply_updates` expects,
and re-raises its errors tagged with the field that caused them so the form can
mark the right input rather than showing one message above everything.
"""
from __future__ import annotations

import math
from typing import Any

from jobhunt import preferences as prefs_module
from jobhunt.preferences import PreferenceError, Preferences

# What the max-age dropdown offers, and what one of each is worth in days.
AGE_UNITS: dict[str, float] = {
    "hours": 1 / 24,
    "days": 1,
    "weeks": 7,
    "months": 30,
}

TOP_N_MAX = 200

# Which form field each preference key belongs to, so an error lands on the
# input the person actually touched.
_FIELD_OF_KEY = {
    "experience": "experience_min",
    "experience_max": "experience_max",
    "locations": "locations",
    "titles": "titles",
    "work_model": "work_model",
    "job_types": "job_types",
    "sources": "sources",
    "min_salary": "min_salary",
    "currency": "currency",
    "max_age_days": "max_age",
    "top_n": "top_n",
    # The dotted keys match the update strings `apply_updates` raises against.
    # The space-separated ones match the label text `Preferences._restricted`
    # embeds in its own error messages, which never carries the dot.
    "upwork.queries": "upwork.queries",
    "upwork.job_types": "upwork.job_types",
    "upwork job type": "upwork.job_types",
    "upwork.min_hourly": "upwork.min_hourly",
    "upwork.min_fixed": "upwork.min_fixed",
    "upwork.experience_level": "upwork.experience_level",
    "upwork experience level": "upwork.experience_level",
    "upwork.verified_payment_only": "upwork.verified_payment_only",
    "upwork.sort": "upwork.sort",
    "upwork sort": "upwork.sort",
    "upwork.require_verified_client": "upwork.require_verified_client",
    "upwork.client_locations": "upwork.client_locations",
    "upwork.client_min_hires": "upwork.client_min_hires",
    "upwork.client_max_hires": "upwork.client_max_hires",
    "upwork.proposals_max": "upwork.proposals_max",
    "upwork.client_min_spend": "upwork.client_min_spend",
    "upwork.recommended_feed": "upwork.recommended_feed",
    "upwork.workload": "upwork.workload",
    "upwork workload": "upwork.workload",
}

# Upwork preference fields the panel edits as free-form lists vs. rate floors.
# Kept apart from `_FIELD_OF_KEY` because they drive flattening, not error
# lookup: `_updates_from` walks these, `_field_for` only reads the map above.
_UPWORK_LIST_FIELDS = ("queries", "job_types", "experience_level", "workload", "client_locations")
_UPWORK_RATE_FIELDS = ("min_hourly", "min_fixed")
# Whole numbers of 0 or more. Zero is meaningful here - client_max_hires=0 is
# "clients who have never hired" - so unlike a rate floor it is never "none".
_UPWORK_COUNT_FIELDS = ("client_min_hires", "client_max_hires", "proposals_max")


class FieldError(ValueError):
    """A rejected value, named against the field that carried it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def _dedupe(values: list[Any]) -> list[str]:
    """Keep the first spelling of each value, compared case-insensitively."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out


def _days_from(payload: Any) -> int:
    if not isinstance(payload, dict):
        raise FieldError("max_age", "max age needs an amount and a unit")
    unit = str(payload.get("unit", "days")).lower()
    if unit not in AGE_UNITS:
        raise FieldError(
            "max_age", f"unknown unit {unit!r}. use one of: {', '.join(AGE_UNITS)}"
        )
    try:
        amount = float(payload.get("value"))
    except (TypeError, ValueError):
        raise FieldError("max_age", "max age must be a number") from None
    if amount <= 0:
        raise FieldError("max_age", "max age must be above zero")
    # A posting eleven hours old is still today's. Round up rather than down,
    # so a tighter window never silently drops the newest jobs.
    return max(1, math.ceil(amount * AGE_UNITS[unit]))


_UPWORK_FLAG_FIELDS = (
    "verified_payment_only",
    "require_verified_client",
    "recommended_feed",
)


def _upwork_updates(payload: dict[str, Any]) -> dict[str, str]:
    """Flatten the nested `upwork` block into the same dotted vocabulary.

    Rate floors are validated here, not left to `apply_updates`: `_number`'s
    own error text ("'x' is not a number") names no field, so a value bad
    enough to raise there would have nothing for `_field_for` to key off.
    """
    updates: dict[str, str] = {}

    for field in _UPWORK_LIST_FIELDS:
        if field in payload:
            updates[f"upwork.{field}"] = ", ".join(_dedupe(payload[field] or [])) or "none"

    for field in _UPWORK_RATE_FIELDS:
        if field not in payload:
            continue
        raw = payload[field]
        if raw in (None, "", "any"):
            updates[f"upwork.{field}"] = "none"
            continue
        try:
            amount = prefs_module._number(str(raw))
        except PreferenceError as exc:
            raise FieldError(f"upwork.{field}", str(exc)) from exc
        if amount <= 0:
            raise FieldError(f"upwork.{field}", "a rate floor has to be above zero")
        updates[f"upwork.{field}"] = f"{amount:g}"

    for field in _UPWORK_COUNT_FIELDS:
        if field not in payload:
            continue
        raw = payload[field]
        if raw in (None, ""):
            updates[f"upwork.{field}"] = "none"
            continue
        text = str(raw).strip()
        if not text.isdigit():
            raise FieldError(f"upwork.{field}", "a whole number, 0 or more")
        updates[f"upwork.{field}"] = text

    if "client_min_spend" in payload:
        raw = payload["client_min_spend"]
        if raw in (None, ""):
            updates["upwork.client_min_spend"] = "none"
        else:
            try:
                amount = prefs_module._number(str(raw))
            except PreferenceError as exc:
                raise FieldError("upwork.client_min_spend", str(exc)) from exc
            if amount < 0:
                raise FieldError("upwork.client_min_spend", "a minimum spend can't be negative")
            updates["upwork.client_min_spend"] = f"{amount:g}" if amount else "none"

    if "sort" in payload:
        updates["upwork.sort"] = str(payload["sort"] or "").strip().lower() or "none"

    for field in _UPWORK_FLAG_FIELDS:
        if field in payload:
            updates[f"upwork.{field}"] = str(bool(payload[field])).lower()

    return updates


def _updates_from(payload: dict[str, Any]) -> dict[str, str]:
    """Flatten the browser payload into the CLI's own update vocabulary."""
    updates: dict[str, str] = {}

    if "titles" in payload:
        updates["titles"] = ",".join(_dedupe(payload["titles"] or [])) or "none"
    if "locations" in payload:
        updates["locations"] = ",".join(_dedupe(payload["locations"] or [])) or "none"
    if "work_model" in payload:
        updates["work_model"] = ",".join(payload["work_model"] or []) or "none"
    if "job_types" in payload:
        updates["job_types"] = ",".join(payload["job_types"] or []) or "none"
    if "sources" in payload:
        updates["sources"] = ",".join(payload["sources"] or []) or "none"
    if "experience_min" in payload:
        updates["experience"] = str(payload["experience_min"] or "none")
    if "experience_max" in payload:
        updates["experience_max"] = str(payload["experience_max"] or "none")
    if "currency" in payload:
        updates["currency"] = str(payload["currency"] or "none")
    if "include_unstated_salary" in payload:
        updates["include_unstated_salary"] = str(bool(payload["include_unstated_salary"]))

    if "min_salary" in payload:
        raw = payload["min_salary"]
        if raw in (None, "", "any"):
            updates["min_salary"] = "none"
        else:
            try:
                amount = prefs_module._number(str(raw))
            except PreferenceError as exc:
                raise FieldError("min_salary", str(exc)) from exc
            if amount <= 0:
                raise FieldError("min_salary", "a salary floor has to be above zero")
            updates["min_salary"] = str(amount)

    if "max_age" in payload:
        updates["max_age_days"] = str(_days_from(payload["max_age"]))

    if isinstance(payload.get("upwork"), dict):
        updates.update(_upwork_updates(payload["upwork"]))

    if "top_n" in payload:
        try:
            count = int(payload["top_n"])
        except (TypeError, ValueError):
            raise FieldError("top_n", "how many jobs to show must be a whole number") from None
        if count < 1 or count > TOP_N_MAX:
            raise FieldError("top_n", f"pick a number between 1 and {TOP_N_MAX}")
        updates["top_n"] = str(count)

    return updates


def _field_for(message: str, updates: dict[str, str]) -> str:
    """Work out which input a PreferenceError belongs to.

    The message names the offending setting; the ceiling rule names both levels
    and belongs on the ceiling, which is the field the person can move. Patterns
    are tried longest first: "experience" is a substring of the upwork label
    "upwork experience level", and a short key must not win that match just
    because it happens to be contained in a longer, more specific one.
    """
    if "ceiling" in message:
        return "experience_max"
    patterns = sorted({*updates, *_FIELD_OF_KEY}, key=len, reverse=True)
    for pattern in patterns:
        if pattern in message:
            return _FIELD_OF_KEY.get(pattern, pattern)
    if "could not place" in message:
        return "locations"
    return "filters"


def apply(prefs: Preferences, payload: dict[str, Any]) -> Preferences:
    """Return `prefs` with `payload` applied, or raise FieldError."""
    updates = _updates_from(payload)
    try:
        return prefs_module.apply_updates(prefs, updates)
    except PreferenceError as exc:
        raise FieldError(_field_for(str(exc), updates), str(exc)) from exc
