"""Safety helpers for the Neckrone8x Stories site. Needs only Flask.

What this adds
--------------
* Fails at start-up (instead of running with a known secret) if SECRET_KEY or
  the admin login is missing on Render.
* Cross-site form protection: a POST is only accepted when the browser says it
  came from this same site (Origin / Referer check) + SameSite cookies.
* Login: constant-time check, 5 wrong tries per 15 minutes, 8 hour sessions,
  optional ADMIN_PASSWORD_HASH instead of a plain password.
* Visitors are told apart by a random cookie (not by IP address: on mobile
  networks in Kenya many people share one IP) and nothing identifying is
  stored: only a keyed hash.
* Rate limits for comments, reactions, submissions and the story feed.
* Helpers: clean text, category whitelist, safe slugs, excerpts, feed token.
"""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
from collections import deque
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import abort, current_app, g, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

CATEGORIES = ["General", "Confession", "Experience", "News", "Relationship",
              "Marriage", "Life", "Novels", "18+", "Other"]
RESERVED_SLUGS = {"submit", "new", "admin", "api", "feed", "sitemap", "robots",
                  "static", "stories", "login", "logout", "health"}
ADMIN_SESSION_HOURS = 8
VISITOR_COOKIE = "n8x_vid"

TITLE_MAX, AUTHOR_MAX, CONTENT_MAX = 200, 100, 200_000
COMMENT_MAX, COMMENT_NAME_MAX = 2000, 80


def is_production() -> bool:
    return bool(os.getenv("RENDER")) or os.getenv("FLASK_ENV") == "production"


# ───────────────────────── set-up ─────────────────────────

def configure_security(app) -> None:
    prod = is_production()
    key = os.getenv("SECRET_KEY", "")
    if prod:
        if len(key) < 24:
            raise RuntimeError(
                "SECRET_KEY must be set (24 or more characters) on Render")
        if not os.getenv("ADMIN_USERNAME"):
            raise RuntimeError("ADMIN_USERNAME must be set on Render")
        if not (os.getenv("ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD_HASH")):
            raise RuntimeError(
                "ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH) must be set on Render")
    app.secret_key = key or "neckrone8x-local-development-only"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=prod,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=ADMIN_SESSION_HOURS),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    # Render / Cloudflare sit in front: trust the forwarded scheme and host so
    # that links, share buttons and secure cookies use https.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.before_request(_same_site_posts_only)
    app.after_request(_add_headers)


_SAFE = {"GET", "HEAD", "OPTIONS"}


def _same_site_posts_only():
    """A form post must come from this site. Browsers always send Origin (or
    at least Referer) on a post, so a page on another site cannot trigger an
    admin action such as 'delete story' for a logged-in admin."""
    if request.method in _SAFE:
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if urlparse(source).netloc.lower() != request.host.lower():
        abort(403)
    return None


def _add_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.path.startswith("/admin"):
        h["Cache-Control"] = "no-store"
    new_vid = getattr(g, "new_vid", None)
    if new_vid:
        resp.set_cookie(VISITOR_COOKIE, new_vid, max_age=365 * 24 * 3600,
                        httponly=True, samesite="Lax", secure=is_production())
    return resp


# ───────────────────────── who is the visitor ─────────────────────────

def _valid_ip(text: str):
    try:
        return str(ipaddress.ip_address((text or "").strip()))
    except ValueError:
        return None


def client_ip() -> str:
    """Cloudflare sets CF-Connecting-IP. Otherwise the last X-Forwarded-For
    entry (added by the host in front of us) is used; earlier entries can be
    written by the visitor."""
    cf = _valid_ip(request.headers.get("CF-Connecting-IP", ""))
    if cf:
        return cf
    for part in reversed([p for p in request.headers.get(
            "X-Forwarded-For", "").split(",") if p.strip()]):
        ip = _valid_ip(part)
        if ip:
            return ip
    return _valid_ip(request.remote_addr or "") or "unknown"


def _keyed_hash(label: str, value: str) -> str:
    key = (current_app.secret_key or "").encode("utf-8")
    return hmac.new(key, f"{label}:{value}".encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


_VID_RE = re.compile(r"[A-Za-z0-9_-]{20,32}")


def visitor_key() -> str:
    """A private, stable id for this browser. A random cookie is the source, so
    two people sharing one phone-network IP are two visitors. Only a keyed hash
    of it is ever stored."""
    vid = request.cookies.get(VISITOR_COOKIE, "")
    if not _VID_RE.fullmatch(vid):
        vid = secrets.token_urlsafe(16)
        g.new_vid = vid  # the cookie is set when the response is sent
    return _keyed_hash("vid", vid)


def ip_fingerprint() -> str:
    """A keyed hash of the IP, only used for rate limits (never the raw IP)."""
    return _keyed_hash("ip", client_ip())


# ───────────────────────── rate limits ─────────────────────────

class RateLimiter:
    """Sliding window, kept in memory (one gunicorn worker)."""

    def __init__(self, now=time.monotonic, max_keys: int = 20000):
        self._now, self._max = now, max_keys
        self._hits = {}

    def count(self, key: str, window: float) -> int:
        q = self._hits.get(key)
        if not q:
            return 0
        cutoff = self._now() - window
        while q and q[0] <= cutoff:
            q.popleft()
        return len(q)

    def allow(self, key: str, limit: int, window: float) -> bool:
        """Records one use. False when the limit is already reached."""
        if self.count(key, window) >= limit:
            return False
        self._hits.setdefault(key, deque()).append(self._now())
        if len(self._hits) > self._max:
            self._purge(window)
        return True

    def add(self, key: str) -> None:
        self._hits.setdefault(key, deque()).append(self._now())

    def _purge(self, window: float) -> None:
        for k in [k for k in self._hits if self.count(k, window) == 0]:
            self._hits.pop(k, None)


limiter = RateLimiter()
_failed_logins = RateLimiter()


def rate_ok(name: str, limit: int, window: float) -> bool:
    return limiter.allow(f"{name}:{ip_fingerprint()}", limit, window)


# ───────────────────────── admin login ─────────────────────────

def login_blocked() -> bool:
    return _failed_logins.count(f"login:{ip_fingerprint()}", 900) >= 5


def login_failed() -> None:
    _failed_logins.add(f"login:{ip_fingerprint()}")


def check_admin_credentials(username: str, password: str) -> bool:
    prod = is_production()
    exp_user = os.getenv("ADMIN_USERNAME") or ("" if prod else "admin")
    pw_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
    exp_pw = os.getenv("ADMIN_PASSWORD") or ("" if prod or pw_hash else "admin123")
    user_ok = bool(exp_user) and hmac.compare_digest(
        username.encode("utf-8"), exp_user.encode("utf-8"))
    if pw_hash:
        try:
            pw_ok = check_password_hash(pw_hash, password)
        except Exception:
            pw_ok = False
    elif exp_pw:
        pw_ok = hmac.compare_digest(password.encode("utf-8"),
                                    exp_pw.encode("utf-8"))
    else:
        pw_ok = False
    return user_ok and pw_ok


def start_admin_session() -> None:
    session.clear()
    session["admin_logged_in"] = True
    session["admin_at"] = int(time.time())
    session.permanent = True


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        started = session.get("admin_at", 0)
        if (not session.get("admin_logged_in")
                or time.time() - started > ADMIN_SESSION_HOURS * 3600):
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


# ───────────────────────── text helpers ─────────────────────────

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value, max_len: int) -> str:
    return _CONTROL.sub("", (value or "")).strip()[:max_len]


def canonical_category(value):
    """The exact category name, or None if it is not one of ours."""
    v = (value or "").strip().lower()
    for c in CATEGORIES:
        if c.lower() == v:
            return c
    return None


def clean_category(value) -> str:
    return canonical_category(value) or "General"


def make_slug(title: str, exists) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:80].strip("-")
    slug = slug or "story"
    if slug in RESERVED_SLUGS:
        slug += "-story"
    base, n = slug, 2
    while exists(slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


def make_excerpt(text: str, limit: int = 240) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0] or t[:limit]
    return cut.rstrip(" ,;:.-") + "…"


def reading_minutes(text: str) -> int:
    return max(1, round(len((text or "").split()) / 200))


def safe_https_url(value, max_len: int = 500):
    v = (value or "").strip()
    if not v or len(v) > max_len:
        return None
    p = urlparse(v)
    return v if p.scheme == "https" and p.netloc else None


def like_escape(text: str) -> str:
    """So a search for 100% or a_b is taken literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def feed_authorized(headers) -> bool:
    """If FEED_TOKEN is set, the story feed needs it (Mkuu's server has it)."""
    token = os.getenv("FEED_TOKEN", "")
    if not token:
        return True
    return hmac.compare_digest((headers.get("X-Feed-Token", "") or "").encode(),
                               token.encode())
