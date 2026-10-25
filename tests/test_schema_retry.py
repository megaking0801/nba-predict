"""ensure_schema must survive a deadlock from concurrent view readers (site build)."""
import psycopg2.errors
import pytest

from jobs import schema


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql is schema.VIEWS and self.conn.fail_views > 0:
            self.conn.fail_views -= 1
            raise psycopg2.errors.DeadlockDetected("deadlock detected")
        self.conn.executed.append(sql)


class _Conn:
    def __init__(self, fail_views):
        self.fail_views = fail_views
        self.executed = []
        self.rollbacks = 0
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(schema.time, "sleep", lambda s: None)


def test_ensure_schema_retries_deadlock_then_succeeds():
    conn = _Conn(fail_views=2)
    schema.ensure_schema(conn)
    assert conn.rollbacks == 2
    assert conn.commits == 1
    assert schema.VIEWS in conn.executed


def test_ensure_schema_gives_up_after_max_attempts():
    conn = _Conn(fail_views=99)
    with pytest.raises(psycopg2.errors.DeadlockDetected):
        schema.ensure_schema(conn)
    assert conn.commits == 0
    assert conn.rollbacks == schema.SCHEMA_RETRIES
