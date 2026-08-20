"""Currency rates for the salary rule.

A salary floor only means something against one snapshot of rates. Every job in
a run has to be compared against the same numbers, or two identical postings in
different currencies get different answers depending on when they were reached.
"""
from __future__ import annotations

import datetime as dt
import json

from jobhunt.web import fx as fx_module


def fetcher(payload):
    def fetch(base):
        if isinstance(payload, Exception):
            raise payload
        return payload

    return fetch


LIVE = {"USD": 1.0, "EUR": 0.918, "TRY": 48.02}


def test_a_live_fetch_is_cached_for_the_next_run(cfg):
    snapshot = fx_module.load(cfg, fetch=fetcher(LIVE))

    assert snapshot.source == "live"
    assert json.loads(fx_module.cache_path(cfg).read_text())["rates"]["TRY"] == 48.02


def test_a_failed_fetch_falls_back_to_the_cache(cfg):
    fx_module.load(cfg, fetch=fetcher(LIVE))

    snapshot = fx_module.load(cfg, fetch=fetcher(ConnectionError("offline")), max_age_hours=0)

    assert snapshot.source == "cache"
    assert snapshot.rates["TRY"] == 48.02


def test_with_no_cache_and_no_network_there_is_no_snapshot(cfg):
    snapshot = fx_module.load(cfg, fetch=fetcher(ConnectionError("offline")))

    assert snapshot.source == "none"
    assert snapshot.usable is False


def test_a_fresh_cache_is_used_without_refetching(cfg):
    fx_module.load(cfg, fetch=fetcher(LIVE))
    calls = []

    def counting(base):
        calls.append(base)
        return LIVE

    fx_module.load(cfg, fetch=counting, max_age_hours=24)

    assert calls == [], "a snapshot from minutes ago does not need refetching"


def test_conversion_goes_through_the_base_currency(cfg):
    snapshot = fx_module.load(cfg, fetch=fetcher(LIVE))

    assert snapshot.convert(60000, "USD", "TRY") == 2881200


def test_converting_to_the_same_currency_changes_nothing(cfg):
    snapshot = fx_module.load(cfg, fetch=fetcher(LIVE))

    assert snapshot.convert(60000, "EUR", "EUR") == 60000


def test_an_unknown_currency_cannot_be_converted(cfg):
    snapshot = fx_module.load(cfg, fetch=fetcher(LIVE))

    assert snapshot.convert(100, "USD", "XYZ") is None


def test_an_unusable_snapshot_converts_nothing(cfg):
    snapshot = fx_module.load(cfg, fetch=fetcher(ConnectionError("offline")))

    assert snapshot.convert(100, "USD", "EUR") is None


def test_a_snapshot_reports_how_old_it_is(cfg):
    snapshot = fx_module.load(cfg, fetch=fetcher(LIVE))

    age = dt.datetime.now(dt.UTC) - snapshot.fetched_at
    assert age.total_seconds() < 60


def test_a_corrupt_cache_is_treated_as_no_cache(cfg):
    fx_module.cache_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    fx_module.cache_path(cfg).write_text("{not json", encoding="utf-8")

    snapshot = fx_module.load(cfg, fetch=fetcher(ConnectionError("offline")))

    assert snapshot.source == "none"
