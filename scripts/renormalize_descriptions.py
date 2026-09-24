#!/usr/bin/env python3
"""Regenerate `description_text` from stored `description_html` with the fixed
`jobhunt.pipeline.normalize.html_to_text` (paragraph/block boundaries preserved
as newlines instead of collapsing into one run-on line).

No network, no re-fetch: every row's `description_html` was already stored by
a prior sync, so this only re-runs a pure text transform over data already in
the database. Rows with empty `description_html` are skipped outright -
personio never stores one (a separate, pre-existing gap, not this script's to
fix) and there is nothing to regenerate from for such a row.

`jd_quality_score` is refreshed (recomputed with `quality.assess` against the
new `description_text`), not merely cleared, for every row whose text changes.
`applications.apply` independently guards against a future stale verdict by
recomputing whenever a cached score is missing or below the gate (see the
step-3 fix), so clearing to NULL here would also have worked; refreshing is
chosen instead because it makes the before/after gate count this script prints
an honest measurement of the fix's actual effect, rather than a count that
reads as "solved" only because the column is temporarily empty.

Standalone: `python scripts/renormalize_descriptions.py [--db PATH] [--batch-size N] [--dry-run]`.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from jobhunt.config import load as load_config  # noqa: E402
from jobhunt.db.models import Job  # noqa: E402
from jobhunt.db.session import session_scope  # noqa: E402
from jobhunt.extract.quality import assess as assess_quality  # noqa: E402
from jobhunt.pipeline.normalize import html_to_text  # noqa: E402

DEFAULT_BATCH_SIZE = 500
QUALITY_GATE = 0.5


def _count_below_gate(db_path: pathlib.Path) -> int:
    with session_scope(db_path) as session:
        return session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.jd_quality_score.is_not(None), Job.jd_quality_score < QUALITY_GATE)
        ).scalar_one()


def run(db_path: pathlib.Path, batch_size: int, dry_run: bool) -> int:
    before_below_gate = _count_below_gate(db_path)

    with session_scope(db_path) as session:
        total = session.execute(select(func.count()).select_from(Job)).scalar_one()

    print(f"db={db_path} rows={total} batch_size={batch_size} dry_run={dry_run}")
    print(f"before: {before_below_gate} rows below the {QUALITY_GATE} quality gate")

    changed = 0
    skipped_empty = 0
    unchanged = 0
    refreshed_scores = 0
    would_be_below_gate = 0
    last_id = 0

    while True:
        with session_scope(db_path) as session:
            batch = session.execute(
                select(Job)
                .where(Job.id > last_id)
                .order_by(Job.id)
                .limit(batch_size)
            ).scalars().all()
            if not batch:
                break
            last_id = batch[-1].id

            for job in batch:
                if not job.description_html:
                    skipped_empty += 1
                    if (job.jd_quality_score or 0) < QUALITY_GATE:
                        would_be_below_gate += 1
                    continue

                new_text = html_to_text(job.description_html)
                if new_text == (job.description_text or ""):
                    unchanged += 1
                    if (job.jd_quality_score or 0) < QUALITY_GATE:
                        would_be_below_gate += 1
                    continue

                changed += 1
                new_score = assess_quality(new_text).score
                if new_score < QUALITY_GATE:
                    would_be_below_gate += 1
                if not dry_run:
                    job.description_text = new_text
                    job.jd_quality_score = new_score
                    refreshed_scores += 1
                    session.add(job)
            # Per-batch commit (session_scope commits on clean exit): a run
            # over 21k rows must never hold one lock for the whole pass, and a
            # crash partway through must leave the batches already done, done.

        print(f"  processed through id={last_id}: changed={changed} unchanged={unchanged} "
              f"skipped_empty={skipped_empty}")

    after_below_gate = would_be_below_gate if dry_run else _count_below_gate(db_path)
    print(f"after: {after_below_gate} rows below the {QUALITY_GATE} quality gate"
          + (" (dry-run preview: nothing written)" if dry_run else ""))
    print(f"total: changed={changed} unchanged={unchanged} skipped_empty={skipped_empty} "
          f"refreshed_scores={refreshed_scores}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=pathlib.Path, default=None,
                        help="Path to the jobhunt sqlite database (default: from config)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing anything")
    args = parser.parse_args()

    db_path = args.db or load_config().db_path
    return run(db_path, args.batch_size, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
