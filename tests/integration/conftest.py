"""PostgreSQL fixtures for integration tests.

`kernel_db` creates a fresh, uniquely named database on the configured server, runs the
real Alembic migrations against it, yields an engine, and drops the database afterwards.
The developer's `elide` database is never written to. Skipped (not passed) when
PostgreSQL is unreachable.
"""

import os
import uuid
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from api.config import get_settings

DEFAULT_URL = "postgresql+psycopg://elide:elide_local_dev_only@127.0.0.1:5433/elide"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _server_url():
    return make_url(os.environ.get("ELIDE_TEST_DATABASE_URL", DEFAULT_URL))


def migrate(url: str, target: str = "head", direction: str = "up") -> None:
    """Run Alembic against `url` (env.py reads DATABASE_URL through settings)."""
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        cfg = Config(os.path.join(ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
        (command.upgrade if direction == "up" else command.downgrade)(cfg, target)
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        get_settings.cache_clear()


@contextmanager
def throwaway_database(pool_size: int = 12):
    """Create a uniquely named database, migrate it to head, yield (engine, url), drop it."""
    server = _server_url()
    admin = create_engine(server, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"PostgreSQL not reachable ({type(exc).__name__}); run docker compose up -d db")
    name = f"elide_test_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = server.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, pool_size=pool_size, max_overflow=4)
    try:
        migrate(url)
        yield engine, url
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def kernel_db():
    with throwaway_database() as (engine, _url):
        yield engine


@pytest.fixture
def fresh_db():
    """A database of its own for tests that need a clean journal (yields engine, url)."""
    with throwaway_database(pool_size=6) as pair:
        yield pair


@pytest.fixture
def uid():
    """Unique suffix so tests sharing the session database never collide."""
    return uuid.uuid4().hex[:8]


# --- P4 workspace harness ----------------------------------------------------------------
class Actor:
    """One merchant user with its own cookie jar (a separate browser session)."""

    def __init__(self, app, login: str, password: str):
        from fastapi.testclient import TestClient

        self.login, self.password = login, password
        self.client = TestClient(app)
        r = self.client.post("/v1/auth/login", json={"login": login, "password": password})
        assert r.status_code == 200, r.text
        self.session = r.json()
        self.csrf = self.session["csrf_token"]

    def get(self, path: str, **kw):
        return self.client.get(path, **kw)

    def post(self, path: str, json=None, *, key: str | None = None, csrf: bool = True, **kw):
        headers = dict(kw.pop("headers", {}))
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        if key:
            headers["Idempotency-Key"] = key
        return self.client.post(path, json=json, headers=headers, **kw)

    def reauth(self):
        r = self.post("/v1/auth/reauth", json={"password": self.password})
        assert r.status_code == 200, r.text


class Workspace:
    def __init__(self, app, engine, clock, seeded):
        self.app, self.engine, self.clock, self.seeded = app, engine, clock, seeded

    @property
    def now(self):
        return self.clock[0]

    def advance(self, **kw):
        from datetime import timedelta

        self.clock[0] = self.clock[0] + timedelta(**kw)

    def actor(self, login: str) -> Actor:
        return Actor(self.app, login, self.seeded.passwords[login])


@pytest.fixture
def workspace(fresh_db):
    """A seeded synthetic workspace on its own database, with a controllable clock."""
    from datetime import UTC, datetime

    from api.app.main import create_app
    from fixtures.workspace_seed import USERS, seed

    engine, _url = fresh_db
    clock = [datetime(2026, 11, 11, 18, 0, tzinfo=UTC)]
    passwords = {login: f"test-only-{i}-{uuid.uuid4().hex[:12]}" for i, login in enumerate(USERS)}
    seeded = seed(engine, now=clock[0], passwords=passwords)
    app = create_app()
    app.state.engine = engine
    app.state.clock = lambda: clock[0]
    yield Workspace(app, engine, clock, seeded)


# --- P5 Sandbox-pilot harness (mock provider; no PayPal call) ------------------------------
PILOT_PAYER_EMAIL = "pilot-payer@sandbox.example.test"
PILOT_PAYEE_MERCHANT_ID = "MOCKSANDBOXPAYEE1"


class Pilot(Workspace):
    """A non-synthetic two-merchant network driven over HTTP, plus the P5 worker against
    the in-memory PayPal double. `work()` runs the outbox like the real worker would."""

    def __init__(self, app, engine, clock, seeded, mock, flags):
        super().__init__(app, engine, clock, seeded)
        self.mock, self.flags = mock, flags
        self.buyer_login, self.seller_login = "pilot-buyer@elide.test", "pilot-seller@elide.test"

    def runtime(self, worker_id: str = "w1", **kw):
        from api.execution.service import Runtime

        return Runtime(
            engine=self.engine,
            provider=kw.pop("provider", self.mock),
            payer_email=lambda: PILOT_PAYER_EMAIL,
            execution_enabled=lambda: self.flags["execution"],
            clock=lambda: self.clock[0],
            worker_id=worker_id,
            **kw,
        )

    def bind(self, *, payee=True, payer=True, invoicer_client_id=None):
        from api.execution import bindings
        from api.integrations.paypal.redaction import fingerprint

        with self.engine.begin() as conn:
            if payee:
                bindings.bind(
                    conn,
                    merchant_id="pilot_seller",
                    role="invoicer",
                    credential_fingerprint=fingerprint(invoicer_client_id or self.mock.client_id),
                    account_fingerprint=fingerprint(PILOT_PAYEE_MERCHANT_ID),
                    consent_note="TEST: owner consents to collect agreed obligations via PayPal",
                    bound_by="test",
                    at=self.now,
                    settlement_currency="USD",
                )
            if payer:
                bindings.bind(
                    conn,
                    merchant_id="pilot_buyer",
                    role="payer",
                    credential_fingerprint=fingerprint(PILOT_PAYER_EMAIL),
                    account_fingerprint=fingerprint(PILOT_PAYER_EMAIL),
                    consent_note="TEST: Sandbox personal payer account",
                    bound_by="test",
                    at=self.now,
                )

    def work(self, rounds: int = 20, step_s: float = 61.0, worker_id: str = "w1", **kw):
        """Run the worker until idle, advancing the clock past retry/poll delays."""
        from datetime import timedelta

        from api.execution.runtime import worker_kinds
        from api.execution.service import handlers_for, on_dead
        from api.workers.outbox import RetryPolicy, run_once

        rt = self.runtime(worker_id, **kw)
        seen = []
        idle = 0
        for _ in range(rounds):
            done = run_once(
                self.engine,
                handlers_for(rt),
                worker_id=worker_id,
                clock=lambda: self.clock[0],
                policy=RetryPolicy(base_s=30, cap_s=120),
                on_dead=lambda j, e: on_dead(rt, j, e),
                kinds=worker_kinds(self.flags["execution"]),
            )
            seen += done
            if done:
                idle = 0
                continue
            idle += 1
            if idle > 3:
                break
            self.clock[0] = self.clock[0] + timedelta(seconds=step_s)
        return seen

    def agree_and_approve(self, key: str = "a"):
        """Buyer accepts the seller's offer, plans, approves its own leg, creates an intent."""
        buyer = self.actor(self.buyer_login)
        offers = buyer.get("/v1/m/pilot_buyer/offers").json()["offers"]
        o = next(x for x in offers if x["status"] == "proposed")
        r = buyer.post(
            f"/v1/m/pilot_buyer/offers/{o['offer_id']}/decision",
            {"decision": "accept", "terms_digest": o["terms_digest"]},
            key=f"accept-offer-{key}",
        )
        assert r.status_code == 200, r.text
        plan = buyer.post("/v1/m/pilot_buyer/plans", {}, key=f"create-plan-{key}")
        assert plan.status_code == 200, plan.text
        leg = next(x for x in plan.json()["plan"]["my_legs"] if x["obligation_id"] == "pilot_001")
        auth = buyer.post(
            f"/v1/m/pilot_buyer/plans/{plan.json()['plan']['plan_id']}/authorizations",
            {
                "leg_id": leg["leg_id"],
                "decision": "approve",
                "binding_digest": leg["authorize"]["binding_digest"],
            },
            key=f"authorize-leg-{key}",
        )
        assert auth.status_code == 200, auth.text
        pi = buyer.post(
            "/v1/m/pilot_buyer/payment-intents",
            {"authorization_id": auth.json()["authorization_id"]},
            key=f"payment-intent-{key}",
        )
        assert pi.status_code == 200, pi.text
        return buyer, pi.json()

    def intent(self, intent_id: str) -> dict:
        from sqlalchemy import select

        from api.workflow.tables import payment_intent

        with self.engine.connect() as c:
            return dict(
                c.execute(select(payment_intent).where(payment_intent.c.intent_id == intent_id))
                .mappings()
                .one()
            )

    def invoice_id(self, intent_id: str) -> str | None:
        from sqlalchemy import select

        from api.execution.tables import payment_session

        with self.engine.connect() as c:
            return c.execute(
                select(payment_session.c.provider_invoice_id).where(
                    payment_session.c.intent_id == intent_id
                )
            ).scalar_one_or_none()


def _pilot(fresh_db, obligations: int):
    from datetime import UTC, datetime

    from api.app.main import create_app
    from api.integrations.paypal.mock import MockPayPal
    from fixtures.sandbox_pilot import USERS, seed_pilot

    engine, _url = fresh_db
    clock = [datetime(2026, 11, 11, 18, 0, tzinfo=UTC)]
    passwords = {login: f"test-only-{i}-{uuid.uuid4().hex[:12]}" for i, login in enumerate(USERS)}
    seeded = seed_pilot(
        engine,
        now=clock[0],
        passwords=passwords,
        attest_buyer=(20000, 5000),
        obligations=obligations,
    )
    flags = {"execution": True}
    mock = MockPayPal(clock=lambda: clock[0], payee_merchant_id=PILOT_PAYEE_MERCHANT_ID)
    app = create_app()
    app.state.engine = engine
    app.state.clock = lambda: clock[0]
    app.state.payments_execution_enabled = True
    app.state.payment_provider = mock
    app.state.payer_email = lambda: PILOT_PAYER_EMAIL
    return Pilot(app, engine, clock, seeded, mock, flags)


@pytest.fixture
def pilot(fresh_db):
    yield _pilot(fresh_db, 1)


@pytest.fixture
def pilot_two(fresh_db):
    """The pilot network with two independent obligations (pilot_001, pilot_002)."""
    yield _pilot(fresh_db, 2)
