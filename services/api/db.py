"""Database session management."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models import Base


def _load_env() -> None:
    """Read repo-root .env without requiring python-dotenv at import time."""
    for parent in (Path(__file__).resolve().parents):
        env = parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
            return


_load_env()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql://"):
    # SQLAlchemy 2 + psycopg 3
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

def _engine_kwargs(url: str) -> dict:
    """
    Pool settings differ by driver.

    Neon is serverless and idles connections aggressively, so a pooled
    Postgres engine needs pre-ping — otherwise it hands out a socket the far
    end has already closed. SQLite (used by the test suite and by offline
    tooling) rejects pool_size/max_overflow outright with a TypeError at
    create_engine time, so those must not be passed unconditionally.
    """
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        return {
            # Keep one in-memory connection alive across sessions; otherwise
            # every session sees a fresh, empty database.
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
            "future": True,
        }
    return {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "future": True,
    }


engine = create_engine(DATABASE_URL, **_engine_kwargs(DATABASE_URL))

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()
