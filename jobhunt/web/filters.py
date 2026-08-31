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
}


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
    and belongs on the ceiling, which is the field the person can move.
    """
    if "ceiling" in message:
        return "experience_max"
    for key in updates:
        if key in message:
            return _FIELD_OF_KEY.get(key, key)
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
