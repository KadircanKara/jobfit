"""Company boards of employers hiring in Turkey, picked by hand.

PLAN.md section 3.5 keeps hand-typed companies out of discovery, with one
exception: an optional list the user chose deliberately. This is that list.
The strategies that find boards on their own (Common Crawl, YC, apply-URL
harvesting) see few Turkish employers, so the tr_local market gets almost
nothing from them.

Every board was checked live on 2026-10-04 against its ATS's public API, and
kept only where most of its open jobs were in Turkey. Boards that mix
countries (trendyol, insiderone, codeway) are kept too: the tr_local filter
drops the jobs located elsewhere.

Left out on purpose:
- lever/peakgames: every job's location field says "Full-time", so no posting
  resolves to a country and the tr_local filter would drop them all.
- greenhouse/insider, greenhouse/peak, greenhouse/gram, ashby/agave,
  recruitee/ace, smartrecruiters/n11: those tokens belong to companies abroad
  with the same name.

A hand-picked board is fetched on every run, like a feed (board_scope.ALWAYS).
"""
from __future__ import annotations

import dataclasses

from sqlalchemy import select

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board
from jobhunt.db.session import session_scope

MARKET = "tr_local"
DISCOVERED_VIA = "manual"

# (provider, token, company). The company name is only for the notes column.
BOARDS: tuple[tuple[str, str, str], ...] = (
    ("lever", "trendyol", "Trendyol"),
    ("lever", "insiderone", "Insider"),
    ("lever", "dreamgames", "Dream Games"),
    ("lever", "spyke-games", "Spyke Games"),
    ("lever", "iyzico", "iyzico"),
    ("lever", "picus", "Picus Security"),
    ("lever", "ciceksepeti", "Çiçeksepeti"),
    ("lever", "commencis", "Commencis"),
    ("ashby", "codeway", "Codeway"),
    ("ashby", "biggergames", "Bigger Games"),
    ("ashby", "agavegames", "Agave Games"),
    ("greenhouse", "goodjobgames", "Good Job Games"),
    ("greenhouse", "dreamgames", "Dream Games"),
    ("recruitee", "obilet", "obilet"),
)


@dataclasses.dataclass
class TurkeyResult:
    boards: int = 0
    new: int = 0
    moved: int = 0
    known: int = 0

    def summary(self) -> str:
        return (
            f"turkey: registered={self.boards} new={self.new} "
            f"moved={self.moved} known={self.known}"
        )


def seed(config: Config, dry_run: bool = False) -> TurkeyResult:
    """Register every board in BOARDS. Idempotent.

    A board another strategy already found is moved onto this list: Common
    Crawl files every board it sees under global_remote, where an Istanbul
    on-site job fails the remote rules, so a Turkish employer left there would
    never surface. Its jobs take the new market on their next fetch.
    """
    result = TurkeyResult(boards=len(BOARDS))
    with session_scope(config.db_path) as session:
        for provider, token, company in BOARDS:
            board = session.scalars(
                select(Board).where(Board.provider == provider, Board.token == token)
            ).first()
            if board is None:
                result.new += 1
                if dry_run:
                    continue
                board = store.get_or_create_board(session, provider, token, DISCOVERED_VIA, MARKET)
            elif board.market == MARKET and board.discovered_via == DISCOVERED_VIA:
                result.known += 1
                continue
            else:
                result.moved += 1
                if dry_run:
                    continue
                board.market = MARKET
                board.discovered_via = DISCOVERED_VIA
            board.notes = f"hand-picked Turkish employer: {company}"
    return result
