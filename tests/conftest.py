"""Runs once before all tests: recreate the test database schema from the migrations."""
import os

import pytest

from app.db import create_pool, migrate

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://ach:ach@localhost:5432/ach_test")


@pytest.fixture(scope="session", autouse=True)
def fresh_schema():
    pool = create_pool(TEST_DATABASE_URL, 2)
    try:
        with pool.connection() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        migrate(pool)
    finally:
        pool.close()
    yield


@pytest.fixture
def harnesses():
    """Creates harnesses on demand and always shuts them down after the test."""
    from tests.harness import Harness
    created: list[Harness] = []

    def make(**kwargs) -> Harness:
        h = Harness(**kwargs)
        created.append(h)
        return h

    yield make
    for h in created:
        h.close()


@pytest.fixture
def h(harnesses):
    return harnesses()


@pytest.fixture
def receivers():
    from tests.harness import Receiver
    created: list[Receiver] = []

    def make(respond=lambda n: 200) -> Receiver:
        r = Receiver(respond)
        created.append(r)
        return r

    yield make
    for r in created:
        r.close()
