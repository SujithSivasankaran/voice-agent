"""
Versioned database migration runner.

Why this exists
---------------
The app talks to Postgres through Supabase's PostgREST client, which cannot run DDL.
For a product shipped as one isolated instance per customer, "paste the schema into
the Supabase SQL editor" does not scale to pushing an upgrade across many live
instances. This runner applies ordered SQL files in `migrations/` exactly once each,
recording what has run in a `schema_migrations` table, so every deploy converges to
the same schema automatically.

How it connects
---------------
DDL needs a direct Postgres connection, not the PostgREST service key. Set one of:
    DATABASE_URL           (standard)         e.g. postgresql://user:pass@host:5432/db
    SUPABASE_DB_URL        (Supabase alias)
Supabase exposes this under Project Settings → Database → Connection string.

If no DB URL is configured, the runner does nothing and prints instructions, so the
legacy manual workflow (run supabase_schema.sql by hand) still works. This makes the
migration step safe to wire into startup unconditionally.

Usage
-----
    python migrate.py            # apply all pending migrations
    python migrate.py --status   # list applied/pending without changing anything
"""

import glob
import hashlib
import logging
import os
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrate")

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

# A fixed 64-bit key so concurrent instances (web + agent booting together) serialise
# on the same Postgres advisory lock and never apply a migration twice.
_ADVISORY_LOCK_KEY = 8163264128  # arbitrary constant, unique to this app


def _db_url() -> str:
    return (os.environ.get("DATABASE_URL", "").strip()
            or os.environ.get("SUPABASE_DB_URL", "").strip())


def _discover() -> list:
    """Ordered list of (version, path). Files are named NNNN_description.sql."""
    paths = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
    out = []
    for p in paths:
        version = os.path.basename(p).split("_", 1)[0]
        out.append((version, p))
    return out


def _connect(url: str):
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "psycopg2 is required to run migrations. Add psycopg2-binary to "
            "requirements.txt (already listed) and reinstall."
        ) from exc
    return psycopg2.connect(url)


def _ensure_tracking_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            filename   TEXT NOT NULL,
            checksum   TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def _applied_versions(cur) -> dict:
    cur.execute("SELECT version, checksum FROM schema_migrations;")
    return {row[0]: row[1] for row in cur.fetchall()}


def status() -> int:
    url = _db_url()
    if not url:
        logger.warning("No DATABASE_URL / SUPABASE_DB_URL set — cannot report status.")
        return 0
    conn = _connect(url)
    try:
        with conn:
            with conn.cursor() as cur:
                _ensure_tracking_table(cur)
                applied = _applied_versions(cur)
        for version, path in _discover():
            mark = "applied" if version in applied else "PENDING"
            logger.info("  %s  %s  [%s]", version, os.path.basename(path), mark)
    finally:
        conn.close()
    return 0


def migrate() -> int:
    url = _db_url()
    if not url:
        logger.warning(
            "No DATABASE_URL / SUPABASE_DB_URL set — skipping automatic migrations.\n"
            "    Provision the schema manually by running the files in migrations/ "
            "(or supabase_schema.sql) in the Supabase SQL editor."
        )
        return 0

    migrations = _discover()
    if not migrations:
        logger.info("No migration files found in %s", MIGRATIONS_DIR)
        return 0

    conn = _connect(url)
    conn.autocommit = False
    applied_count = 0
    try:
        with conn.cursor() as cur:
            # Serialise concurrent runners; the lock is released when the tx ends.
            cur.execute("SELECT pg_advisory_xact_lock(%s);", (_ADVISORY_LOCK_KEY,))
            _ensure_tracking_table(cur)
            applied = _applied_versions(cur)

            for version, path in migrations:
                with open(path, "r", encoding="utf-8") as fh:
                    sql = fh.read()
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()

                if version in applied:
                    if applied[version] != checksum:
                        logger.warning(
                            "Migration %s already applied but its file changed since "
                            "(checksum mismatch). Not re-running; create a NEW migration "
                            "for further changes.", version,
                        )
                    continue

                logger.info("Applying migration %s (%s) …", version, os.path.basename(path))
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum) "
                    "VALUES (%s, %s, %s);",
                    (version, os.path.basename(path), checksum),
                )
                applied_count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Migration failed — rolled back. No partial changes were kept.")
        return 1
    finally:
        conn.close()

    if applied_count:
        logger.info("Applied %d migration(s).", applied_count)
    else:
        logger.info("Database is up to date - nothing to apply.")
    return 0


if __name__ == "__main__":
    if "--status" in sys.argv:
        raise SystemExit(status())
    raise SystemExit(migrate())
