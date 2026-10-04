"""Tier 2 aggregator feeds, registered as boards like everything else.

An aggregator is not keyed by a company token, but making it a Board row anyway
means it goes through the same scheduler, the same per-run cap, the same
degraded handling, and the same raw storage as an ATS board. One fetch path, not
two. PLAN.md non-negotiable 8.

Every feed here was verified live on 2026-08-20 (references/sources.md), except
where its entry says otherwise.
"""
from __future__ import annotations

import dataclasses

from sqlalchemy import select

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board
from jobhunt.db.session import session_scope

# (provider, token, market). "all" means the source's whole feed.
FEEDS: tuple[tuple[str, str, str], ...] = (
    ("remotive", "all", "global_remote"),
    ("remoteok", "all", "global_remote"),
    ("arbeitnow", "all", "global_remote"),
    ("jobicy", "all", "global_remote"),
    ("wwr", "all", "global_remote"),
    ("wwr", "remote-programming-jobs", "global_remote"),
    ("wwr", "remote-devops-sysadmin-jobs", "global_remote"),
    # Verified 2026-10-04. See sources/techcareer.py.
    ("techcareer", "all", "tr_local"),
)

DISCOVERED_VIA = "feed"


@dataclasses.dataclass
class FeedResult:
    feeds: int = 0
    new: int = 0
    known: int = 0

    def summary(self) -> str:
        return f"feeds: registered={self.feeds} new={self.new} known={self.known}"


def seed(config: Config, dry_run: bool = False) -> FeedResult:
    """Register every known feed as a board. Idempotent."""
    result = FeedResult(feeds=len(FEEDS))
    with session_scope(config.db_path) as session:
        for provider, token, market in FEEDS:
            existing = session.scalars(
                select(Board).where(Board.provider == provider, Board.token == token)
            ).first()
            if existing is not None:
                result.known += 1
                continue
            result.new += 1
            if dry_run:
                continue
            board = store.get_or_create_board(session, provider, token, DISCOVERED_VIA, market)
            board.notes = "tier 2 aggregator feed"
    return result
