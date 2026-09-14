"""Shared pytest fixtures.

Gate tests get a fresh in-memory SQLite database per test. A StaticPool is
used so the single in-memory connection is shared across the engine for
the test's lifetime.

Dial-script tests get a seeded file-backed database in a temp directory,
dummy LiveKit credentials, and a recording fake in place of the LiveKit
API client, so scripts/dial.py can be driven end to end with no network.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from livekit_outbound import predial
from livekit_outbound.gate.db import Base, open_session
from livekit_outbound.gate.models import Contact
from livekit_outbound.gate.seed import seed

DIAL_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dial.py"

# Inside the calling window everywhere in the continental US and Canada.
MIDDAY_UTC = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    """A fresh in-memory SQLite engine with schema created, per test."""
    engine = create_engine(
        "sqlite://",  # in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def db_session(engine) -> Generator[Session, None, None]:
    """A SQLAlchemy session bound to the per-test in-memory engine."""
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


# --- scripts/dial.py harness -------------------------------------------------


@pytest.fixture(scope="session")
def dial_module():
    """scripts/dial.py imported as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("dial_script", DIAL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeRoomService:
    async def create_room(self, request):
        return SimpleNamespace(name=request.name)


class _FakeDispatchService:
    def __init__(self) -> None:
        self.requests = []

    async def create_dispatch(self, request):
        self.requests.append(request)
        return SimpleNamespace(id="AD_fake", agent_name=request.agent_name)


class _FakeSipService:
    def __init__(self) -> None:
        self.requests = []

    async def create_sip_participant(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            participant_identity=request.participant_identity, participant_id="PA_fake"
        )


class FakeLiveKitAPI:
    """Records every dial request instead of making one."""

    instances: list["FakeLiveKitAPI"] = []

    def __init__(self, url: str, api_key: str, api_secret: str) -> None:
        self.room = _FakeRoomService()
        self.agent_dispatch = _FakeDispatchService()
        self.sip = _FakeSipService()
        self.closed = False
        FakeLiveKitAPI.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def gate_db(tmp_path, monkeypatch) -> str:
    """A seeded file-backed gate database, wired in via GATE_DATABASE_URL."""
    url = f"sqlite:///{(tmp_path / 'gate.db').as_posix()}"
    monkeypatch.setenv("GATE_DATABASE_URL", url)
    session = open_session(url)
    try:
        seed(session)
    finally:
        session.close()
    return url


@pytest.fixture
def dial_env(monkeypatch, dial_module) -> type[FakeLiveKitAPI]:
    """Dummy LiveKit settings and the recording fake in place of the client."""
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.invalid")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIfake")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "fakesecret")
    monkeypatch.setenv("SIP_OUTBOUND_TRUNK_ID", "ST_fake")
    monkeypatch.setenv("AGENT_NAME", "outbound-caller")
    FakeLiveKitAPI.instances.clear()
    monkeypatch.setattr(dial_module.api, "LiveKitAPI", FakeLiveKitAPI)
    return FakeLiveKitAPI


@pytest.fixture
def fixed_wall_clock(monkeypatch) -> datetime:
    """Pin the pre-dial path's wall clock to MIDDAY_UTC (not an override:
    the gate sees this as the real current time)."""
    monkeypatch.setattr(predial, "utcnow", lambda: MIDDAY_UTC)
    return MIDDAY_UTC


def rows(url: str, model):
    """All rows of ``model`` in the file-backed gate database, by id."""
    session = open_session(url, create_schema=False)
    try:
        return list(session.scalars(select(model).order_by(model.id)).all())
    finally:
        session.close()


def engagement_id_for(url: str, contact_name: str) -> int:
    session = open_session(url, create_schema=False)
    try:
        contact = session.scalar(select(Contact).where(Contact.name == contact_name))
        (engagement,) = contact.engagements
        return engagement.id
    finally:
        session.close()
