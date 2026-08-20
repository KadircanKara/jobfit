"""Engine and session factory."""
from __future__ import annotations

import pathlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from jobhunt.db.models import Base

_engines: dict[str, Engine] = {}


def get_engine(db_path: pathlib.Path | str) -> Engine:
    url = f"sqlite:///{db_path}" if str(db_path) != ":memory:" else "sqlite://"
    if url not in _engines:
        engine = create_engine(url, future=True)

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        _engines[url] = engine
    return _engines[url]


def create_all(db_path: pathlib.Path | str) -> None:
    if str(db_path) != ":memory:":
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(get_engine(db_path))


@contextmanager
def session_scope(db_path: pathlib.Path | str) -> Iterator[Session]:
    factory = sessionmaker(bind=get_engine(db_path), future=True, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upgrade_to_head(db_path: pathlib.Path | str) -> str:
    """Run alembic migrations. Used by `jobhunt init` so create_all and alembic
    can never disagree about what the schema is."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    root = pathlib.Path(__file__).resolve().parent.parent.parent
    cfg = AlembicConfig(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "jobhunt" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(cfg, "head")
    return "head"
