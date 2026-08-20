"""Board discovery. PLAN.md section 3.5.

The user never types a company name. Tokens come from URLs the pipeline already
ingests (Strategy A), from public company lists, from Common Crawl, and from
certificate transparency. Discovery writes to the `boards` table; fetching reads
from it. That table, not a YAML file, is the source of truth.
"""
from __future__ import annotations

from jobhunt.discovery.patterns import BoardHit, DomainHint, scan, scan_many

__all__ = ["BoardHit", "DomainHint", "scan", "scan_many"]
