"""Tests for site_security.py with a real (tiny) Flask app.
Run from the project folder:  python -m unittest discover -s tests -v"""
import os
import re
import time
import unittest

from flask import Flask, jsonify, redirect, request, session, url_for

import site_security as S

ENV_KEYS = ("RENDER", "FLASK_ENV", "SECRET_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD",
            "ADMIN_PASSWORD_HASH", "FEED_TOKEN")


def make_app():
    app = Flask(__name__)
    S.configure_security(app)

    @app.route("/")
    def index():
        return "home"

    @app.route("/post", methods=["POST"])
    def post():
        return "posted"

    @app.route("/vid")
    def vid():
        return jsonify(key=S.visitor_key(), ip=S.client_ip(), fp=S.ip_fingerprint())

    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if S.login_blocked():
                return "blocked", 429
            if S.check_admin_credentials(request.form.get("u", ""), request.form.get("p", "")):
                S.start_admin_session()
                return redirect("/admin/secret")
            S.login_failed()
            return "bad", 200
        return "form"

    @app.route("/admin/secret")
    @S.admin_required
    def secret():
        return "secret"

    return app


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
        S.limiter._hits.clear(); S._failed_logins._hits.clear()

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


class StartupTests(Base):
    def test_render_without_secrets_refuses_to_start(self):
        os.environ["RENDER"] = "true"
        with self.assertRaises(RuntimeError):
            make_app()                                  # no SECRET_KEY
        os.environ["SECRET_KEY"] = "x" * 30
        with self.assertRaises(RuntimeError):
            make_app()                                  # no admin user
        os.environ["ADMIN_USERNAME"] = "boss"
        with self.assertRaises(RuntimeError):
            make_app()                                  # no admin password
        os.environ["ADMIN_PASSWORD"] = "a-long-password"
        make_app()                                      # now fine

    def test_short_secret_is_refused_in_production(self):
        os.environ.update(RENDER="1", SECRET_KEY="short", ADMIN_USERNAME="a", ADMIN_PASSWORD="b")
        with self.assertRaises(RuntimeError):
            make_app()

    def test_local_development_still_works_without_anything(self):
        app = make_app()
        self.assertEqual(app.test_client().get("/").status_code, 200)

    def test_cookie_settings(self):
        os.environ.update(RENDER="1", SECRET_KEY="k" * 30, ADMIN_USERNAME="a", ADMIN_PASSWORD="pw")
        app = make_app()
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])


class SameSiteTests(Base):
    def setUp(self):
        super().setUp()
        self.c = make_app().test_client()

    def test_posts_from_this_site_are_fine(self):
        r = self.c.post("/post", headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        r = self.c.post("/post", headers={"Referer": "http://localhost/some/page"})
        self.assertEqual(r.status_code, 200)

    def test_posts_from_another_site_or_without_proof_are_refused(self):
        self.assertEqual(self.c.post("/post", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.c.post("/post", headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(self.c.post("/post").status_code, 403)
        self.assertEqual(self.c.post("/post", headers={"Referer": "https://evil.example/x"}).status_code, 403)

    def test_reading_pages_is_never_blocked(self):
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_security_headers_and_admin_pages_are_not_cached(self):
        r = self.c.get("/")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("Referrer-Policy", r.headers)
        self.assertEqual(self.c.get("/admin/login").headers["Cache-Control"], "no-store")


class VisitorTests(Base):
    def setUp(self):
        super().setUp()
        self.app = make_app()

    def test_cookie_is_issued_once_and_the_key_is_stable(self):
        c = self.app.test_client()
        r1 = c.get("/vid")
        self.assertIn("n8x_vid=", r1.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", r1.headers["Set-Cookie"])
        r2 = c.get("/vid")
        self.assertNotIn("n8x_vid=", r2.headers.get("Set-Cookie", ""))   # not re-issued
        self.assertEqual(r1.get_json()["key"], r2.get_json()["key"])

    def test_two_browsers_on_the_same_ip_are_two_visitors(self):
        h = {"CF-Connecting-IP": "41.90.1.2"}
        a = self.app.test_client().get("/vid", headers=h).get_json()
        b = self.app.test_client().get("/vid", headers=h).get_json()
        self.assertNotEqual(a["key"], b["key"])
        self.assertEqual(a["fp"], b["fp"])                 # but the same IP fingerprint

    def test_nothing_stored_contains_the_raw_ip_or_cookie(self):
        out = self.app.test_client().get("/vid", headers={"CF-Connecting-IP": "41.90.1.2"}).get_json()
        self.assertRegex(out["key"], r"^[0-9a-f]{32}$")
        self.assertNotIn("41.90.1.2", out["key"] + out["fp"])

    def test_garbage_cookie_is_replaced(self):
        c = self.app.test_client()
        c.set_cookie("n8x_vid", "x")
        self.assertIn("n8x_vid=", c.get("/vid").headers.get("Set-Cookie", ""))

    def test_client_ip_rules(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/vid", headers={"CF-Connecting-IP": "41.1.1.1", "X-Forwarded-For": "9.9.9.9"}).get_json()["ip"], "41.1.1.1")
        self.assertEqual(c.get("/vid", headers={"X-Forwarded-For": "6.6.6.6, 41.2.2.2"}).get_json()["ip"], "41.2.2.2")
        self.assertEqual(c.get("/vid", headers={"CF-Connecting-IP": "junk"}).get_json()["ip"], "127.0.0.1")


class LoginTests(Base):
    def setUp(self):
        super().setUp()
        os.environ.update(ADMIN_USERNAME="boss", ADMIN_PASSWORD="correct horse")
        self.c = make_app().test_client()
        self.h = {"Origin": "http://localhost"}

    def login(self, u, p, ip="41.1.1.1"):
        return self.c.post("/admin/login", data={"u": u, "p": p},
                           headers={**self.h, "CF-Connecting-IP": ip})

    def test_good_login_opens_the_admin_area(self):
        self.assertEqual(self.c.get("/admin/secret").status_code, 302)    # locked before
        r = self.login("boss", "correct horse")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/admin/secret").data, b"secret")

    def test_wrong_password_or_name_fails(self):
        self.assertEqual(self.login("boss", "nope").data, b"bad")
        self.assertEqual(self.login("admin", "correct horse").data, b"bad")

    def test_five_wrong_tries_block_even_the_right_password(self):
        for _ in range(5):
            self.login("boss", "guess")
        self.assertEqual(self.login("boss", "correct horse").status_code, 429)
        self.assertEqual(self.login("boss", "correct horse", ip="41.9.9.9").status_code, 302)  # others unaffected

    def test_default_admin_password_does_not_work_when_a_real_one_is_set(self):
        self.assertEqual(self.login("admin", "admin123").data, b"bad")

    def test_hashed_password_is_supported(self):
        from werkzeug.security import generate_password_hash
        os.environ.pop("ADMIN_PASSWORD")
        os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash("s3cret!")
        self.assertEqual(self.login("boss", "s3cret!").status_code, 302)
        c2 = make_app().test_client()
        r = c2.post("/admin/login", data={"u": "boss", "p": "wrong"}, headers=self.h)
        self.assertEqual(r.data, b"bad")

    def test_session_expires_after_eight_hours(self):
        self.login("boss", "correct horse")
        with self.c.session_transaction() as s:
            s["admin_at"] = int(time.time()) - 9 * 3600
        self.assertEqual(self.c.get("/admin/secret").status_code, 302)

    def test_login_form_post_from_another_site_is_refused(self):
        r = self.c.post("/admin/login", data={"u": "boss", "p": "correct horse"},
                        headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


class RateLimiterTests(unittest.TestCase):
    def test_window_slides(self):
        t = [100.0]
        r = S.RateLimiter(now=lambda: t[0])
        self.assertTrue(all(r.allow("k", 3, 60) for _ in range(3)))
        self.assertFalse(r.allow("k", 3, 60))
        self.assertTrue(r.allow("other", 3, 60))
        t[0] += 61
        self.assertTrue(r.allow("k", 3, 60))

    def test_memory_stays_bounded(self):
        t = [0.0]
        r = S.RateLimiter(now=lambda: t[0], max_keys=50)
        for i in range(500):
            r.allow(f"k{i}", 1, 10); t[0] += 11
        self.assertLess(len(r._hits), 100)


class TextTests(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(S.clean_text("  hi\x00 there\x07  ", 50), "hi there")
        self.assertEqual(S.clean_text("a" * 500, 10), "a" * 10)
        self.assertEqual(S.clean_text(None, 10), "")
        self.assertEqual(S.clean_text("line1\nline2\tx", 50), "line1\nline2\tx")

    def test_category_whitelist(self):
        self.assertEqual(S.clean_category("confession"), "Confession")
        self.assertEqual(S.clean_category("18+"), "18+")
        self.assertEqual(S.clean_category("<script>"), "General")
        self.assertIsNone(S.canonical_category("%"))

    def test_slugs(self):
        taken = {"my-story", "my-story-2"}
        self.assertEqual(S.make_slug("My Story!", taken.__contains__), "my-story-3")
        self.assertEqual(S.make_slug("???", set().__contains__), "story")
        self.assertEqual(S.make_slug("Submit", set().__contains__), "submit-story")   # would hide the submit page
        self.assertLessEqual(len(S.make_slug("x" * 500, set().__contains__)), 80)

    def test_excerpt_and_reading_time(self):
        self.assertEqual(S.make_excerpt("short text"), "short text")
        long = "word " * 200
        ex = S.make_excerpt(long, 50)
        self.assertTrue(ex.endswith("…"))
        self.assertLessEqual(len(ex), 51)
        self.assertFalse(ex[:-1].endswith(" "))
        self.assertEqual(S.reading_minutes("w " * 650), 3)
        self.assertEqual(S.reading_minutes(""), 1)

    def test_urls_and_like_escape(self):
        self.assertEqual(S.safe_https_url("https://res.cloudinary.com/x/a.jpg"), "https://res.cloudinary.com/x/a.jpg")
        for bad in ("http://x.com/a.jpg", "javascript:alert(1)", "//x.com/a", "", None, "https://" + "a" * 600):
            self.assertIsNone(S.safe_https_url(bad), bad)
        self.assertEqual(S.like_escape("100%_x\\"), "100\\%\\_x\\\\")

    def test_the_full_story_text_always_needs_the_token(self):
        os.environ.pop("FEED_TOKEN", None)
        self.assertFalse(S.feed_authorized_strict({}))                     # no token set: closed
        self.assertFalse(S.feed_authorized_strict({"X-Feed-Token": ""}))
        os.environ["FEED_TOKEN"] = "tok-123"
        try:
            self.assertFalse(S.feed_authorized_strict({}))
            self.assertFalse(S.feed_authorized_strict({"X-Feed-Token": "wrong"}))
            self.assertTrue(S.feed_authorized_strict({"X-Feed-Token": "tok-123"}))
        finally:
            os.environ.pop("FEED_TOKEN", None)

    def test_feed_token(self):
        os.environ.pop("FEED_TOKEN", None)
        self.assertTrue(S.feed_authorized({}))
        os.environ["FEED_TOKEN"] = "tok-123"
        try:
            self.assertFalse(S.feed_authorized({}))
            self.assertFalse(S.feed_authorized({"X-Feed-Token": "wrong"}))
            self.assertTrue(S.feed_authorized({"X-Feed-Token": "tok-123"}))
        finally:
            os.environ.pop("FEED_TOKEN", None)


if __name__ == "__main__":
    unittest.main()
