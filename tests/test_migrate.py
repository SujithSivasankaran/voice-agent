"""Migration runner discovery/config — no database required."""

import os

import migrate


def test_discover_orders_migrations():
    found = migrate._discover()
    versions = [v for v, _ in found]
    assert versions == sorted(versions), "migrations must be discovered in version order"
    # The baseline and the two feature migrations should be present.
    assert "0001" in versions
    assert "0002" in versions
    assert "0003" in versions


def test_db_url_prefers_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://a")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://b")
    assert migrate._db_url() == "postgresql://a"


def test_db_url_falls_back_to_supabase(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://b")
    assert migrate._db_url() == "postgresql://b"


def test_migrate_noop_without_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    # With no DB URL configured, migrate() is a graceful no-op returning 0.
    assert migrate.migrate() == 0
