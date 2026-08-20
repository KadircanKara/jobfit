#!/usr/bin/env python3
"""Standalone endpoint smoke test for jobhunt sources.

Probes every endpoint listed in PLAN.md section 3, records the real response
shape to data/probe/<name>.json, and prints one line per probe.

Usage:
    python3 scripts/probe_sources.py [--only NAME] [--timeout SECONDS]

Exit code is 0 if every probe returned a usable payload, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

import httpx

UA = "jobhunt-probe/0.1 (personal job search tool; kadircann.kara@gmail.com)"
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "probe"

# name -> (method, url, json_body_or_None, note)
PROBES: dict[str, tuple[str, str, dict[str, Any] | None, str]] = {
    # Tier 1, marked verified in the plan. Cheap re-check.
    "greenhouse": ("GET", "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true", None, "token=stripe"),
    "lever": ("GET", "https://api.lever.co/v0/postings/matchgroup?mode=json&limit=5", None, "token=matchgroup"),
    "ashby": ("GET", "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true", None, "token=ramp"),
    "smartrecruiters": ("GET", "https://api.smartrecruiters.com/v1/companies/Visa/postings", None, "companyId=Visa, case sensitive"),
    "recruitee": ("GET", "https://channable.recruitee.com/api/offers/", None, "token=channable"),
    "personio": ("GET", "https://personio.jobs.personio.de/xml?language=en", None, "XML, token=personio"),
    # Tier 1, needs verification.
    "workable": ("GET", "https://apply.workable.com/api/v1/widget/accounts/gohighlevel?details=true", None, "token=gohighlevel"),
    "workday": ("POST", "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
                {"limit": 5, "offset": 0, "searchText": "engineer", "appliedFacets": {}}, "tenant=nvidia wd5"),
    # Tier 2.
    "himalayas_browse": ("GET", "https://himalayas.app/jobs/api?limit=5&offset=0", None, "max 20/req"),
    "himalayas_search": ("GET", "https://himalayas.app/jobs/api/search?q=backend%20engineer&timezone=UTC%2B03%3A00", None, ""),
    "himalayas_openapi": ("GET", "https://himalayas.app/docs/openapi.json", None, ""),
    "remotive": ("GET", "https://remotive.com/api/remote-jobs?limit=5", None, ""),
    "remoteok": ("GET", "https://remoteok.com/api", None, "first element is attribution"),
    "arbeitnow": ("GET", "https://www.arbeitnow.com/api/job-board-api", None, ""),
    "jobicy": ("GET", "https://jobicy.com/api/v2/remote-jobs?count=5", None, ""),
    "wwr_rss": ("GET", "https://weworkremotely.com/categories/remote-programming-jobs.rss", None, "RSS"),
    # Tier 3 and discovery.
    "yc_meta": ("GET", "https://yc-oss.github.io/api/meta.json", None, ""),
    "yc_hiring": ("GET", "https://yc-oss.github.io/api/companies/hiring.json", None, ""),
    "cc_collinfo": ("GET", "https://index.commoncrawl.org/collinfo.json", None, ""),
    "crtsh_recruitee": ("GET", "https://crt.sh/?q=%25.recruitee.com&output=json", None, "slow, rate limited"),
}


def shape(value: Any, depth: int = 0, max_depth: int = 3) -> Any:
    """Describe a JSON value's structure without carrying its content."""
    if depth >= max_depth:
        return type(value).__name__
    if isinstance(value, dict):
        return {k: shape(v, depth + 1, max_depth) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1, max_depth), f"...len={len(value)}"] if value else []
    if isinstance(value, str):
        return f"str(len={len(value)})"
    return type(value).__name__


def probe(client: httpx.Client, name: str, method: str, url: str, body: dict | None, note: str) -> dict:
    started = time.time()
    record: dict[str, Any] = {"name": name, "method": method, "url": url, "note": note}
    try:
        if method == "POST":
            resp = client.post(url, json=body, headers={"Content-Type": "application/json"})
        else:
            resp = client.get(url)
        record["status"] = resp.status_code
        record["content_type"] = resp.headers.get("content-type", "")
        record["bytes"] = len(resp.content)
        record["elapsed_s"] = round(time.time() - started, 2)
        text = resp.text
        if "json" in record["content_type"] or text.lstrip()[:1] in "{[":
            try:
                data = resp.json()
                record["parsed"] = "json"
                record["shape"] = shape(data)
                record["count"] = _count(data)
                record["sample"] = _sample(data)
            except Exception as exc:  # noqa: BLE001
                record["parsed"] = f"json-error: {exc}"
                record["head"] = text[:400]
        else:
            record["parsed"] = "text"
            record["head"] = text[:800]
        record["ok"] = resp.status_code == 200 and record.get("bytes", 0) > 0
    except Exception as exc:  # noqa: BLE001
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def _count(data: Any) -> int | None:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("jobs", "postings", "offers", "data", "results", "content", "jobPostings"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return None


def _sample(data: Any) -> Any:
    """One representative record, truncated."""
    item = None
    if isinstance(data, list) and data:
        item = data[0]
    elif isinstance(data, dict):
        for key in ("jobs", "postings", "offers", "data", "results", "content", "jobPostings"):
            if isinstance(data.get(key), list) and data[key]:
                item = data[key][0]
                break
    if item is None:
        return None
    if isinstance(item, dict):
        return {k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v) for k, v in item.items()}
    return item


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    names = args.only or list(PROBES)
    failures = 0
    with httpx.Client(timeout=args.timeout, follow_redirects=True, headers={"User-Agent": UA, "Accept": "*/*"}) as client:
        for name in names:
            method, url, body, note = PROBES[name]
            record = probe(client, name, method, url, body, note)
            (OUT / f"{name}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str))
            if not record.get("ok"):
                failures += 1
            print(
                f"{'OK ' if record.get('ok') else 'FAIL'} {name:22s} "
                f"status={record.get('status', '-')} bytes={record.get('bytes', 0)} "
                f"count={record.get('count')} {record.get('error', '')}"
            )
            time.sleep(0.7)

    print(f"probe: {len(names) - failures}/{len(names)} ok, artifacts in {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
