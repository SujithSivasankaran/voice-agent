"""Login must never 500 or redirect-loop when the users table is unreachable or a
cookie belongs to a user who no longer exists. No database: lookups are faked."""

import pytest
from itsdangerous import URLSafeTimedSerializer

server = pytest.importorskip("server")
from fastapi.testclient import TestClient  # noqa: E402

auth = server.auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "builtin-pass-123")
    monkeypatch.setattr(auth, "_serializer", URLSafeTimedSerializer("test-secret", salt=auth._SESSION_SALT))
    monkeypatch.setattr(auth, "_principal_cache", {})

    async def admin_name():
        return "admin"

    async def users_table_missing(username):
        raise RuntimeError("relation \"users\" does not exist")

    monkeypatch.setattr(auth, "get_admin_username", admin_name)
    monkeypatch.setattr("db.get_user_by_username", users_table_missing)
    monkeypatch.setattr(server.app.router, "on_startup", [])
    monkeypatch.setattr(server.app.router, "on_shutdown", [])
    with TestClient(server.app) as c:
        yield c


def test_builtin_admin_can_log_in_when_users_table_is_missing(client):
    r = client.post("/login", json={"username": "admin", "password": "builtin-pass-123"})
    assert r.status_code == 200, r.text
    assert client.get("/auth/me").json() == {"username": "admin", "role": "admin"}


def test_unknown_user_gets_401_not_500(client):
    assert client.post("/login", json={"username": "nobody", "password": "whatever-123"}).status_code == 401


def test_stale_cookie_does_not_loop(client):
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token("deleted-user"))
    r = client.get("/login", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 200                      # shows the form instead of bouncing to "/"
    assert auth.SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")   # and clears the bad cookie
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/login")
