-- ═══════════════════════════════════════════════════════
-- 0006 — Dashboard users with roles
--
-- admin    — everything, including spend and managing users
-- employee — everything except spend figures and user management
-- The original ADMIN_USERNAME login (env / settings table) keeps working as the
-- built-in admin and is not stored here.
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee' CHECK (role IN ('admin', 'employee')),
    created_at TEXT NOT NULL
);
ALTER TABLE users DISABLE ROW LEVEL SECURITY;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users (lower(username));
