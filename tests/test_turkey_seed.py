"""The hand-picked Turkish employer boards. No network."""
from __future__ import annotations

from jobhunt import store
from jobhunt.db.models import Board
from jobhunt.db.session import session_scope
from jobhunt.discovery import turkey


def boards(cfg) -> dict[tuple[str, str], Board]:
    with session_scope(cfg.db_path) as session:
        rows = session.query(Board).all()
        session.expunge_all()
    return {(b.provider, b.token): b for b in rows}


def test_every_board_registers_under_tr_local_and_reseeding_is_a_no_op(cfg) -> None:
    first = turkey.seed(cfg)
    assert first.new == len(turkey.BOARDS)

    again = turkey.seed(cfg)
    assert (again.new, again.moved, again.known) == (0, 0, len(turkey.BOARDS))

    rows = boards(cfg)
    assert set(rows) == {(provider, token) for provider, token, _ in turkey.BOARDS}
    assert {b.market for b in rows.values()} == {"tr_local"}
    assert {b.discovered_via for b in rows.values()} == {"manual"}


def test_a_board_common_crawl_filed_as_global_remote_moves_to_tr_local(cfg) -> None:
    """Left under global_remote, an Istanbul on-site job fails the remote rules."""
    with session_scope(cfg.db_path) as session:
        store.get_or_create_board(session, "ashby", "codeway", "commoncrawl", "global_remote")

    result = turkey.seed(cfg)
    assert result.moved == 1

    codeway = boards(cfg)[("ashby", "codeway")]
    assert (codeway.market, codeway.discovered_via) == ("tr_local", "manual")


def test_dry_run_writes_nothing(cfg) -> None:
    assert turkey.seed(cfg, dry_run=True).new == len(turkey.BOARDS)
    assert boards(cfg) == {}
