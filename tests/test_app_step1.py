"""Tests for the real app (step 1 fixes): feed limits, Retry-After, the
submissions token, pool_pre_ping, visitor IP, share image, small page fixes.

Uses a throwaway SQLite file. .env is never loaded (load_dotenv is stubbed
before app.py is imported), so no real database or secret is touched."""
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime

import dotenv

dotenv.load_dotenv = lambda *a, **k: False  # never read .env in tests

_TMP = tempfile.mkdtemp(prefix="stories-tests-")
for _k in ("RENDER", "FLASK_ENV", "FEED_TOKEN", "ADMIN_PASSWORD_HASH"):
    os.environ.pop(_k, None)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only-123456")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import site_security as S  # noqa: E402
import app as app_module  # noqa: E402
from app import Story, app, db  # noqa: E402

TOKEN = "t" * 32
HOST = "http://localhost"
SUBMISSION = {
    "title": "A story from Mkuu",
    "content": "word " * 40,
    "category": "Life",
    "author": "Wanjiru",
    "author_key": "abc123def456",
}


class AppBase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FEED_TOKEN", None)
        S.limiter._hits.clear()
        S._failed_logins._hits.clear()
        app_module.clear_trending_cache()
        self.c = app.test_client()
        with app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        os.environ.pop("FEED_TOKEN", None)

    def add_story(self, **kw):
        data = dict(title="Hello", slug="hello", category="Life",
                    content="Some text " * 30, author="A", published=True,
                    created_at=datetime.utcnow())
        data.update(kw)
        with app.app_context():
            db.session.add(Story(**data))
            db.session.commit()


# ───────────────────────── 1. feed limits + Retry-After ─────────────────────────

class FeedLimitTests(AppBase):
    def test_without_token_the_strict_ip_limit_applies(self):
        # FEED_TOKEN unset: the feed is open, limited per IP (60 / minute)
        for _ in range(S.FEED_IP_LIMIT):
            self.assertEqual(self.c.get("/api/stories").status_code, 200)
        r = self.c.get("/api/stories")
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r.headers.get("Retry-After", "").isdigit())
        self.assertGreaterEqual(int(r.headers["Retry-After"]), 1)
        self.assertLessEqual(int(r.headers["Retry-After"]), S.FEED_WINDOW + 1)

    def test_token_calls_get_their_own_higher_bucket(self):
        os.environ["FEED_TOKEN"] = TOKEN
        h = {"X-Feed-Token": TOKEN}
        self.add_story()
        urls = ["/api/stories", "/api/stories/hello", "/api/stories/hello/text"]
        for i in range(S.FEED_IP_LIMIT * 2):  # well past the per-IP limit
            r = self.c.get(urls[i % 3], headers=h)
            self.assertEqual(r.status_code, 200, (i, r.get_json()))

    def test_token_bucket_is_capped_not_unlimited(self):
        os.environ["FEED_TOKEN"] = TOKEN
        h = {"X-Feed-Token": TOKEN}
        saved = S.FEED_TOKEN_LIMIT
        S.FEED_TOKEN_LIMIT = 5
        try:
            for _ in range(5):
                self.assertEqual(self.c.get("/api/stories", headers=h).status_code, 200)
            r = self.c.get("/api/stories", headers=h)
            self.assertEqual(r.status_code, 429)
            self.assertIn("Retry-After", r.headers)
        finally:
            S.FEED_TOKEN_LIMIT = saved
        self.assertGreater(S.FEED_TOKEN_LIMIT, S.FEED_IP_LIMIT * 10)

    def test_wrong_token_is_refused(self):
        os.environ["FEED_TOKEN"] = TOKEN
        for url in ("/api/stories", "/api/stories/hello/text"):
            self.assertEqual(self.c.get(url).status_code, 401)
            self.assertEqual(self.c.get(url, headers={"X-Feed-Token": "nope"}).status_code, 401)

    def test_retry_after_on_login_and_submit_429(self):
        for _ in range(5):
            self.c.post("/admin/login", data={"username": "x", "password": "y"},
                        headers={"Origin": HOST})
        r = self.c.post("/admin/login", data={"username": "x", "password": "y"},
                        headers={"Origin": HOST})
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["Retry-After"]), 1)

        form = {"title": "T", "content": "C"}
        for _ in range(5):
            self.c.post("/stories/submit", data=form, headers={"Origin": HOST})
        r = self.c.post("/stories/submit", data=form, headers={"Origin": HOST})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)


# ───────────────────────── real visitor IP ─────────────────────────

class ClientIpTests(unittest.TestCase):
    def ip(self, headers):
        with app.test_request_context("/", headers=headers):
            return S.client_ip()

    def test_private_proxy_hop_at_the_end_is_skipped(self):
        # the old code returned "10.0.3.7" here, so every visitor shared it
        self.assertEqual(self.ip({"X-Forwarded-For": "41.90.1.2, 10.0.3.7"}), "41.90.1.2")
        self.assertEqual(self.ip({"X-Forwarded-For": "41.90.1.2, 100.64.0.9"}), "41.90.1.2")
        self.assertEqual(self.ip({"X-Forwarded-For": "41.90.1.2, 172.16.0.1, 127.0.0.1"}), "41.90.1.2")

    def test_rightmost_public_hop_wins_over_typed_left_entries(self):
        self.assertEqual(self.ip({"X-Forwarded-For": "6.6.6.6, 41.2.2.2"}), "41.2.2.2")

    def test_cloudflare_header_only_when_public(self):
        self.assertEqual(self.ip({"CF-Connecting-IP": "41.1.1.1", "X-Forwarded-For": "9.9.9.9"}), "41.1.1.1")
        self.assertEqual(self.ip({"CF-Connecting-IP": "10.0.0.1", "X-Forwarded-For": "41.3.3.3"}), "41.3.3.3")

    def test_two_visitors_behind_one_proxy_get_two_buckets(self):
        a = self.ip({"X-Forwarded-For": "41.90.1.2, 10.0.3.7"})
        b = self.ip({"X-Forwarded-For": "105.160.4.4, 10.0.3.7"})
        self.assertNotEqual(a, b)


# ───────────────────────── 2. /api/submissions token auth ─────────────────────────

class SubmissionsTests(AppBase):
    def test_server_call_with_token_and_no_origin_is_accepted(self):
        os.environ["FEED_TOKEN"] = TOKEN
        r = self.c.post("/api/submissions", json=SUBMISSION, headers={"X-Feed-Token": TOKEN})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["status"], "pending")
        with app.app_context():
            self.assertFalse(Story.query.filter_by(slug=r.get_json()["slug"]).one().published)

    def test_without_or_with_wrong_token_it_is_refused(self):
        os.environ["FEED_TOKEN"] = TOKEN
        for headers in ({}, {"X-Feed-Token": "wrong"}, {"Origin": HOST},
                        {"Origin": "https://evil.example"}):
            r = self.c.post("/api/submissions", json=SUBMISSION, headers=headers)
            self.assertEqual(r.status_code, 401, headers)
        with app.app_context():
            self.assertEqual(Story.query.count(), 0)

    def test_refused_when_no_token_is_configured(self):
        r = self.c.post("/api/submissions", json=SUBMISSION, headers={"X-Feed-Token": ""})
        self.assertEqual(r.status_code, 401)

    def test_other_posts_keep_the_origin_check(self):
        os.environ["FEED_TOKEN"] = TOKEN
        self.add_story()
        h = {"X-Feed-Token": TOKEN}  # the token does NOT open other routes
        for url in ("/stories/hello/comment", "/stories/hello/react/love",
                    "/stories/submit", "/admin/login"):
            self.assertEqual(self.c.post(url, headers=h).status_code, 403, url)
            self.assertEqual(self.c.post(url, headers={"Origin": "https://evil.example"}).status_code, 403, url)
        r = self.c.post("/stories/hello/comment", data={"content": "hi"}, headers={"Origin": HOST})
        self.assertEqual(r.status_code, 302)

    def test_only_the_submissions_route_is_marked(self):
        marked = [name for name, view in app.view_functions.items()
                  if getattr(view, "token_post", False)]
        self.assertEqual(marked, ["api_submissions"])


# ───────────────────────── 3. pool_pre_ping ─────────────────────────

class DatabaseSettingsTests(unittest.TestCase):
    def test_pool_pre_ping(self):
        self.assertTrue(app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_pre_ping"])
        with app.app_context():
            self.assertTrue(db.engine.pool._pre_ping)


# ───────────────────────── 4. page fixes ─────────────────────────

class PageFixTests(AppBase):
    def meta(self, html, attr, name):
        m = re.search(r'<meta %s="%s" content="([^"]*)"' % (attr, re.escape(name)), html)
        return m.group(1) if m else None

    def test_share_image_is_the_full_https_url(self):
        self.add_story(cover_image="https://cdn.example.com/c.jpg")
        html = self.c.get("/stories/hello").get_data(as_text=True)
        self.assertEqual(self.meta(html, "property", "og:image"), "https://cdn.example.com/c.jpg")
        self.assertEqual(self.meta(html, "name", "twitter:image"), "https://cdn.example.com/c.jpg")

    def test_old_relative_cover_becomes_absolute_https(self):
        self.add_story(cover_image="/static/covers/c.jpg")
        html = self.c.get("/stories/hello", base_url="https://stories.example.com").get_data(as_text=True)
        self.assertEqual(self.meta(html, "property", "og:image"), "https://stories.example.com/static/covers/c.jpg")

    def test_no_image_tags_without_a_usable_cover(self):
        for cover in (None, "javascript:alert(1)", "//evil.example/x.jpg"):
            with app.app_context():
                db.session.query(Story).delete()
                db.session.commit()
            self.add_story(cover_image=cover)
            html = self.c.get("/stories/hello").get_data(as_text=True)
            self.assertIsNone(self.meta(html, "property", "og:image"), cover)
            self.assertIsNone(self.meta(html, "name", "twitter:image"), cover)

    def test_logo_footer_and_story_count(self):
        self.add_story()
        html = self.c.get("/stories").get_data(as_text=True)
        self.assertRegex(html, r'href="https://neckrone8x\.co\.ke/"\s+class="logo"')
        self.assertIn("© %d" % datetime.utcnow().year, html)
        self.assertRegex(html, r"\b1 story\b")
        self.assertNotRegex(html, r"\b1 stories\b")
        self.add_story(title="Two", slug="two")
        html = self.c.get("/stories").get_data(as_text=True)
        self.assertRegex(html, r"\b2 stories\b")


if __name__ == "__main__":
    unittest.main()
