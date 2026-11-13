"""Test harness: real PostgreSQL (pgvector) and Redis via Testcontainers.

The database is bootstrapped and migrated exactly like production (roles,
RLS, SECURITY DEFINER functions), and the API under test connects as the
RLS-restricted xplo_app role.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import asyncpg
import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests.support.clamd import FakeClamd

PG_IMAGE = "pgvector/pgvector:pg17"
REDIS_IMAGE = "redis:7.4-alpine"
# S3-compatible object storage for tests and development (MinIO's community edition is archived).
S3_IMAGE = "chrislusf/seaweedfs:4.50"
S3_KEY, S3_SECRET = "test-access-key", "test-secret-key-0000"
CSRF = {"X-Requested-With": "XploWhatsApp"}
PASSWORD = "Correct-Horse-42!"
META_APP_ID = "1234567890"
META_APP_SECRET = "test-app-secret"


@pytest.fixture(scope="session")
def clamd() -> Iterator[FakeClamd]:
    fake = FakeClamd()
    yield fake
    fake.close()


@pytest.fixture(scope="session")
def containers(clamd: FakeClamd) -> Iterator[dict[str, str]]:
    with (
        PostgresContainer(PG_IMAGE, username="postgres", password="postgres", dbname="xplo_test") as pg,
        RedisContainer(REDIS_IMAGE) as redis,
        DockerContainer(S3_IMAGE)
        .with_env("AWS_ACCESS_KEY_ID", S3_KEY)
        .with_env("AWS_SECRET_ACCESS_KEY", S3_SECRET)
        .with_command("mini -dir=/data")
        .with_exposed_ports(8333)
        .waiting_for(LogMessageWaitStrategy("connected to filers").with_startup_timeout(90)) as objects,
    ):
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        base = f"postgresql+asyncpg://{{}}@{host}:{port}/xplo_test"
        env = {
            "ENVIRONMENT": "test",
            "LOG_JSON": "false",
            "LOG_LEVEL": "WARNING",
            "DATABASE_URL": base.format("xplo_app:app-pw"),
            "DATABASE_ADMIN_URL": base.format("xplo_admin:admin-pw"),
            "DATABASE_OWNER_URL": base.format("xplo_owner:owner-pw"),
            "DATABASE_SUPERUSER_URL": base.format("postgres:postgres"),
            "DATABASE_POOL_SIZE": "5",
            "REDIS_URL": f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0",
            "EMAIL_BACKEND": "memory",
            "COOKIE_SECURE": "false",
            "RATE_LIMIT_AUTH_PER_MINUTE": "10000",
            "FRONTEND_BASE_URL": "http://app.test",
            "TASK_DISPATCH": "memory",
            # Platform AI keys exist; tests decide per business whether to use them.
            "OPENAI_API_KEY": "sk-platform-openai-0000000000",
            "GEMINI_API_KEY": "AIza-platform-gemini-000000000000000",
            "DEEPSEEK_API_KEY": "sk-platform-deepseek-000000000",
            "AI_REPLY_DEBOUNCE_SECONDS": "0",
            "META_APP_ID": META_APP_ID,
            "META_APP_SECRET": META_APP_SECRET,
            "META_WEBHOOK_VERIFY_TOKEN": "verify-me",
            "META_EMBEDDED_SIGNUP_CONFIG_ID": "cfg-123",
            "META_GRAPH_API_VERSION": "v25.0",
            "S3_ENDPOINT_URL": f"http://{objects.get_container_host_ip()}:{objects.get_exposed_port(8333)}",
            "S3_ACCESS_KEY_ID": S3_KEY,
            "S3_SECRET_ACCESS_KEY": S3_SECRET,
            "S3_BUCKET": "xplo-test",
            "CLAMAV_HOST": "127.0.0.1",
            "CLAMAV_PORT": str(clamd.port),
            # Payment gateways (answered by the fakes in tests/support/billing.py).
            "RAZORPAY_KEY_ID": "rzp_test_key",
            "RAZORPAY_KEY_SECRET": "rzp-test-secret",
            "RAZORPAY_WEBHOOK_SECRET": "rzp-webhook-secret",
            "STRIPE_SECRET_KEY": "sk_test_stripe",
            "STRIPE_WEBHOOK_SECRET": "whsec_test",
            "SELLER_LEGAL_NAME": "Xplosys Technologies",
            "SELLER_ADDRESS": "1 MG Road, Bengaluru 560001",
            "SELLER_STATE_CODE": "29",
            "SELLER_GSTIN": "29AAACX1234A1ZT",
            "SELLER_LUT_NUMBER": "AD290000000001",
            "INVOICE_SAC_CODE": "998314",
        }
        os.environ.update(env)
        yield env


def _clear_caches() -> None:
    from app.core.config import get_settings
    from app.core.redis import get_redis
    from app.core.security import crypto, jwt
    from app.db import session

    for fn in (
        get_settings,
        get_redis,
        jwt.get_keyset,
        crypto._keks,
        session.get_app_engine,
        session.get_admin_engine,
        session.get_app_sessionmaker,
        session.get_admin_sessionmaker,
    ):
        fn.cache_clear()


def run_alembic(*args: str) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    getattr(command, args[0])(cfg, *args[1:])


@pytest.fixture(scope="session")
async def database(containers: dict[str, str]) -> AsyncIterator[dict[str, str]]:
    _clear_caches()
    from app.cli import _seed_plans
    from app.core.config import get_settings
    from app.db.bootstrap import bootstrap_database

    await bootstrap_database(get_settings())
    await asyncio.to_thread(run_alembic, "upgrade", "head")
    await _seed_plans()
    from app.modules.storage.s3 import ensure_bucket

    for attempt in range(30):  # the S3 gateway may still be connecting to its filer
        try:
            await ensure_bucket()
            break
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(1)
    yield containers
    from app.core.redis import close_redis
    from app.db.session import dispose_engines

    await dispose_engines()
    await close_redis()


def superuser_dsn() -> str:
    return os.environ["DATABASE_SUPERUSER_URL"].replace("postgresql+asyncpg", "postgresql")


@pytest.fixture
async def su_conn(database: dict[str, str]) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(superuser_dsn())
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def clean_state(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    if "database" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("database")
    conn = await asyncpg.connect(superuser_dsn())
    try:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename NOT IN ('alembic_version', 'subscription_plans')"
        )
        if tables:
            await conn.execute("TRUNCATE " + ", ".join(f'"{t["tablename"]}"' for t in tables) + " CASCADE")
    finally:
        await conn.close()
    from app.core.redis import get_redis
    from app.modules.notifications.email import OUTBOX
    from app.workers.dispatch import DISPATCHED

    await get_redis().flushdb()
    OUTBOX.clear()
    DISPATCHED.clear()
    yield


@pytest.fixture(scope="session")
async def app(database: dict[str, str]) -> Any:
    from app.main import create_app

    return create_app()


ClientFactory = Callable[[], httpx.AsyncClient]


@pytest.fixture
async def make_client(app: Any) -> AsyncIterator[ClientFactory]:
    clients: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield factory
    for c in clients:
        await c.aclose()


@pytest.fixture
async def client(make_client: ClientFactory) -> httpx.AsyncClient:
    return make_client()


class Actor:
    """A signed-in user with their own cookie jar and access token."""

    def __init__(self, client: httpx.AsyncClient, session: dict[str, Any]):
        self.client = client
        self.update(session)

    def update(self, session: dict[str, Any]) -> None:
        self.session = session
        self.token = session["access_token"]
        self.user_id = session["user"]["id"]
        self.tenant_id = session["active_tenant_id"]

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.get(url, headers=self.auth, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.post(url, headers=self.auth, **kw)

    async def put(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.put(url, headers=self.auth, **kw)

    async def patch(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.patch(url, headers=self.auth, **kw)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.delete(url, headers=self.auth, **kw)


RegisterFn = Callable[..., Awaitable[Actor]]


@pytest.fixture
def register(make_client: ClientFactory) -> RegisterFn:
    async def _register(email: str, business: str | None = "Acme Clinic", **extra: Any) -> Actor:
        c = make_client()
        body = {"email": email, "password": PASSWORD, "full_name": email.split("@")[0].title(), **extra}
        if business is not None:
            body["business_name"] = business
        r = await c.post("/api/v1/auth/register", json=body)
        assert r.status_code == 201, r.text
        return Actor(c, r.json())

    return _register


def token_from_outbox(fragment: str) -> str:
    from app.modules.notifications.email import OUTBOX

    for mail in reversed(OUTBOX):
        if fragment in mail.text:
            return mail.text.split(fragment, 1)[1].split()[0]
    raise AssertionError(f"No email containing {fragment!r}")
