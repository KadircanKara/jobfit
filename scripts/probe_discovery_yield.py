#!/usr/bin/env python3
"""Measure how many ATS board tokens each aggregator actually leaks.

PLAN.md 3.5 asserts that a Himalayas run yields several dozen distinct tokens.
That is a testable claim and it decides which source is worth building first, so
this script tests it against every verified aggregator instead of assuming it.

Standalone: `python scripts/probe_discovery_yield.py [--only NAME] [--timeout N]`.
Writes data/probe/yield_<name>.json and prints one line per source.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobhunt.discovery import patterns  # noqa: E402

UA = "jobhunt-probe/0.2 (personal job search tool; kadircann.kara@gmail.com)"
OUT = pathlib.Path(__file__).resolve().parents[1] / "data" / "probe"

# name -> (url, json path to the job list, fields to scan)
SOURCES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "himalayas": ("https://himalayas.app/jobs/api?limit=100&offset=0", "jobs", ("applicationLink", "guid", "description")),
    "remotive": ("https://remotive.com/api/remote-jobs?limit=100", "jobs", ("url", "description", "job_type")),
    "remoteok": ("https://remoteok.com/api", "", ("apply_url", "url", "description")),
    "arbeitnow": ("https://www.arbeitnow.com/api/job-board-api", "data", ("url", "description")),
    "jobicy": ("https://jobicy.com/api/v2/remote-jobs?count=50", "jobs", ("url", "jobDescription")),
    "wwr": ("https://weworkremotely.com/remote-jobs.rss", "", ()),
}


def extract(payload: Any, path: str) -> list[dict]:
    if not path:
        records = payload
    else:
        records = payload.get(path, [])
    return [r for r in records if isinstance(r, dict)]


def probe(name: str, timeout: float) -> dict:
    url, path, fields = SOURCES[name]
    headers = {"User-Agent": UA, "Accept": "application/json, text/xml;q=0.9, */*;q=0.8"}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        response = client.get(url)
    result: dict[str, Any] = {"name": name, "url": url, "status": response.status_code}
    if response.status_code != 200:
        return result

    hits: set[patterns.BoardHit] = set()
    hints: set[patterns.DomainHint] = set()

    if not fields:  # RSS or any other blob: scan the whole body
        found, hinted = patterns.scan(response.text)
        hits |= found
        hints |= hinted
        result["records"] = response.text.count("<item>")
    else:
        payload = response.json()
        records = extract(payload, path)
        result["records"] = len(records)
        for record in records:
            found, hinted = patterns.scan_many([str(record.get(f) or "") for f in fields])
            hits |= found
            hints |= hinted

    by_provider: dict[str, int] = {}
    for hit in hits:
        by_provider[hit.provider] = by_provider.get(hit.provider, 0) + 1
    result["distinct_tokens"] = len(hits)
    result["by_provider"] = by_provider
    result["hints"] = len(hints)
    result["sample"] = [f"{h.provider}:{h.token}" for h in sorted(hits)[:20]]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    names = [args.only] if args.only else list(SOURCES)
    failures = 0
    for name in names:
        try:
            result = probe(name, args.timeout)
        except Exception as exc:  # noqa: BLE001 - a probe that dies is a result
            result = {"name": name, "error": f"{type(exc).__name__}: {exc}"}
        (OUT / f"yield_{name}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if "error" in result or result.get("status") != 200:
            failures += 1
            print(f"{name}: FAIL {result.get('error') or result.get('status')}")
            continue
        print(
            f"{name}: records={result['records']} tokens={result['distinct_tokens']} "
            f"hints={result['hints']} {result['by_provider']}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
