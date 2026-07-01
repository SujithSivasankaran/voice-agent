"""
Authentication for the admin dashboard — single-admin, password + signed session cookie.

Design (industry-standard for an internal same-origin dashboard):
- Passwords are bcrypt-hashed; plaintext is never stored.
- The browser session is a signed, timestamped cookie (itsdangerous), set HttpOnly +
  Secure + SameSite so it cannot be read by JavaScript or replayed cross-site. No
  bcrypt runs per request — only the signature is checked.
- Login is rate-limited per client IP to blunt brute-force / credential-stuffing.

Credential resolution (highest precedence first). Env is authoritative when set, so
env changes are never silently shadowed by a stale stored hash:
    1. env         ADMIN_PASSWORD_HASH         (a pre-computed bcrypt hash)
    2. env         ADMIN_PASSWORD              (plaintext; hashed in memory at use)
    3. DB setting  auth_admin_password_hash    (set via the change-password endpoint,
                   or the generated bootstrap below)
    4. generated   a random password, hashed + persisted, printed ONCE to the logs
                   so the dashboard is never left open with no credential.
When ADMIN_PASSWORD/ADMIN_PASSWORD_HASH is set, the password is "env-managed" and the
in-app change-password endpoint is disabled (rotate it in the environment instead).

Session secret resolution:  env SESSION_SECRET  >  DB setting auth_session_secret  >
a generated-and-persisted secret (so restarts don't log the admin out).
"""

import logging
import os
import secrets
import time

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger("outbound-auth")

SESSION_COOKIE_NAME = "session"
_SESSION_SALT = "outboundai.session.v1"

# 12h default session lifetime; tune via env.
try:
    SESSION_MAX_AGE = max(300, int(os.environ.get("SESSION_MAX_AGE_SECONDS", str(12 * 3600))))
except ValueError:
    SESSION_MAX_AGE = 12 * 3600

# Secure cookies require HTTPS. Defaults ON (Coolify terminates TLS); set
# AUTH_COOKIE_SECURE=false only for local http development.
COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "true").strip().lower() != "false"
COOKIE_SAMESITE = os.environ.get("AUTH_COOKIE_SAMESITE", "lax").strip().lower() or "lax"

# Login rate limiting (per client IP).
try:
    LOGIN_MAX_ATTEMPTS = max(1, int(os.environ.get("LOGIN_MAX_ATTEMPTS", "8")))
except ValueError:
    LOGIN_MAX_ATTEMPTS = 8
try:
    LOGIN_WINDOW_SECONDS = max(30, int(os.environ.get("LOGIN_WINDOW_SECONDS", "900")))
except ValueError:
    LOGIN_WINDOW_SECONDS = 900

# Resolved once at startup by init_auth().
_session_secret: str = ""
_serializer: URLSafeTimedSerializer | None = None

# In-memory failed-attempt tracker: ip -> list[timestamp]. Fine for a single instance.
_login_failures: dict[str, list[float]] = {}


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    """bcrypt hash (includes a per-hash random salt). Returns a str for storage."""
    # bcrypt silently truncates beyond 72 bytes; guard so long passwords aren't
    # weakened by silent truncation surprises.
    pw = (plaintext or "").encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    if not plaintext or not hashed:
        return False
    try:
        return bcrypt.checkpw((plaintext or "").encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ── Credential resolution ─────────────────────────────────────────────────────

async def get_admin_username() -> str:
    from db import get_stored_setting
    stored = await get_stored_setting("auth_admin_username")
    return stored or os.environ.get("ADMIN_USERNAME", "").strip() or "admin"


def is_password_env_managed() -> bool:
    """True when the password comes from the environment, so the in-app
    change-password flow is disabled (rotate it in the environment instead)."""
    return bool(os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
                or os.environ.get("ADMIN_PASSWORD", ""))


async def _resolve_password_hash() -> str:
    """Return the current admin bcrypt hash, applying the precedence chain and
    bootstrapping a generated password if nothing is configured."""
    from db import get_stored_setting

    env_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
    if env_hash:
        return env_hash

    env_pw = os.environ.get("ADMIN_PASSWORD", "")
    if env_pw:
        return hash_password(env_pw)

    db_hash = await get_stored_setting("auth_admin_password_hash")
    if db_hash:
        return db_hash

    return await _bootstrap_generated_password()


async def _bootstrap_generated_password() -> str:
    """No credential configured: generate one, persist its hash, print it ONCE so the
    operator can log in, and never leave the dashboard open."""
    from db import get_stored_setting, set_setting

    existing = await get_stored_setting("auth_admin_password_hash")
    if existing:
        return existing

    generated = secrets.token_urlsafe(12)
    hashed = hash_password(generated)
    try:
        await set_setting("auth_admin_password_hash", hashed)
    except Exception as exc:
        logger.error("Could not persist bootstrap admin password: %s", exc)
    logger.warning(
        "\n"
        "════════════════════════════════════════════════════════════════\n"
        " No admin credential configured. A temporary one was generated:\n"
        "     username: %s\n"
        "     password: %s\n"
        " Log in and change it, or set ADMIN_PASSWORD in the environment.\n"
        "════════════════════════════════════════════════════════════════",
        os.environ.get("ADMIN_USERNAME", "admin"), generated,
    )
    return hashed


async def verify_credentials(username: str, password: str) -> bool:
    """Constant-time-ish credential check. Runs bcrypt even on username mismatch to
    avoid leaking which part was wrong via timing."""
    expected_user = await get_admin_username()
    expected_hash = await _resolve_password_hash()
    user_ok = secrets.compare_digest((username or "").strip(), expected_user)
    pw_ok = verify_password(password, expected_hash)
    return user_ok and pw_ok


async def set_admin_password(new_password: str) -> None:
    from db import set_setting
    await set_setting("auth_admin_password_hash", hash_password(new_password))


# ── Session tokens ────────────────────────────────────────────────────────────

async def init_auth() -> None:
    """Resolve/persist the session secret and bootstrap the admin credential.
    Call once on startup, after the DB is reachable."""
    global _session_secret, _serializer

    secret = os.environ.get("SESSION_SECRET", "").strip()
    if not secret:
        from db import get_stored_setting, set_setting
        secret = await get_stored_setting("auth_session_secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            try:
                await set_setting("auth_session_secret", secret)
            except Exception as exc:
                logger.warning("Could not persist session secret (sessions reset on restart): %s", exc)
    _session_secret = secret
    _serializer = URLSafeTimedSerializer(secret, salt=_SESSION_SALT)

    # Surface the generated credential in logs at startup if nothing is configured.
    await _resolve_password_hash()
    logger.info("Auth initialised (session ttl=%ss, secure_cookie=%s)", SESSION_MAX_AGE, COOKIE_SECURE)


def create_session_token(username: str) -> str:
    if _serializer is None:
        raise RuntimeError("init_auth() has not run")
    return _serializer.dumps({"u": username})


def verify_session_token(token: str) -> str | None:
    """Return the username if the token is valid and unexpired, else None."""
    if not token or _serializer is None:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
    if isinstance(data, dict):
        return data.get("u")
    return None


# ── Login rate limiting ───────────────────────────────────────────────────────

def _prune(ip: str, now: float) -> None:
    recent = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if recent:
        _login_failures[ip] = recent
    else:
        _login_failures.pop(ip, None)


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    _prune(ip, now)
    return len(_login_failures.get(ip, [])) >= LOGIN_MAX_ATTEMPTS


def record_failure(ip: str) -> None:
    _login_failures.setdefault(ip, []).append(time.time())


def reset_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def retry_after_seconds(ip: str) -> int:
    now = time.time()
    attempts = _login_failures.get(ip, [])
    if not attempts:
        return 0
    return max(1, int(LOGIN_WINDOW_SECONDS - (now - min(attempts))))


# ── Cookie helpers ────────────────────────────────────────────────────────────

def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite=COOKIE_SAMESITE)


def client_ip(request) -> str:
    """Best-effort client IP. Honour the first X-Forwarded-For hop when behind the
    reverse proxy (Coolify), else the direct peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"
