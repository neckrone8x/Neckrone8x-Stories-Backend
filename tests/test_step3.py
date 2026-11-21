"""Step 3: comment moderation (held / approve / trust link / delete), the
admin form token, and the publish date (separate from the submission date)."""
import re
import unittest
from unittest import mock
from datetime import datetime, timedelta

from test_app_step1 import HOST, AppBase, Story, app, db  # sets up a temp DB

import app as app_module
from app import Comment

ORIGIN = {"Origin": HOST}


class Step3Base(AppBase):
    def login(self):
        r = self.c.post("/admin/login", headers=ORIGIN,
                        data={"username": "admin", "password": "admin123"})
        self.assertEqual(r.status_code, 302)

    def token(self):
        html = self.c.get("/admin/comments").get_data(as_text=True)
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        if m:
            return m.group(1)
        html = self.c.get("/admin/create-story").get_data(as_text=True)  # always has a form
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def comment(self, content, **kw):
        with app.app_context():
            story = Story.query.filter_by(slug=kw.pop("slug", "hello")).one()
            c = Comment(story_id=story.id, name=kw.pop("name", "Amina"), content=content, **kw)
            db.session.add(c)
            db.session.commit()
            return c.id

    def public(self, slug="hello"):
        return self.c.get("/stories/" + slug).get_data(as_text=True)


# ───────────────────────── comments ─────────────────────────

class CommentModerationTests(Step3Base):
    def setUp(self):
        super().setUp()
        self.add_story()

    def test_comment_with_link_is_held_then_shown_after_approve(self):
        r = self.c.post("/stories/hello/comment", headers=ORIGIN,
                        data={"name": "Otieno", "content": "Look at https://spam.example/x"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("comment=held", r.headers["Location"])
        self.assertIn("will appear after a quick review", self.c.get(r.headers["Location"]).get_data(as_text=True))
        self.assertNotIn("spam.example", self.public())             # hidden publicly

        with app.app_context():
            c = Comment.query.one()
            self.assertFalse(c.approved)                             # stored as held, not lost
            cid = c.id

        self.login()
        held = self.c.get("/admin/comments?tab=held").get_data(as_text=True)
        self.assertIn("spam.example", held)
        self.assertIn("Otieno", held)
        self.assertIn('href="/stories/hello#comments"', held)        # story title links to it
        self.assertIn("Approve and trust link", held)
        self.assertNotIn("spam.example", self.c.get("/admin/comments?tab=approved").get_data(as_text=True))

        r = self.c.post("/admin/comments/%d/approve" % cid, headers=ORIGIN,
                        data={"csrf_token": self.token(), "tab": "held"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/admin/comments?tab=held"))
        html = self.public()
        self.assertIn("https://spam.example/x", html)                 # shown...
        self.assertNotIn('href="https://spam.example', html)          # ...as plain text

    def test_approve_and_trust_link_makes_a_safe_link(self):
        cid = self.comment('My blog https://blog.example/a?b=1&c=2. And "quotes" <b>x</b>', approved=False)
        self.login()
        r = self.c.post("/admin/comments/%d/approve-trust" % cid, headers=ORIGIN,
                        data={"csrf_token": self.token()})
        self.assertEqual(r.status_code, 302)
        html = self.public()
        self.assertIn('<a href="https://blog.example/a?b=1&amp;c=2" rel="nofollow ugc noopener noreferrer" '
                      'target="_blank">https://blog.example/a?b=1&amp;c=2</a>.', html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)                   # the rest is still escaped
        self.assertNotIn("<b>x</b>", html)

    def test_trusted_link_cannot_break_out_of_the_attribute(self):
        cid = self.comment('https://x.example/"onmouseover="alert(1) <script>alert(2)</script> javascript:alert(3)',
                           approved=True, link_trusted=True)
        html = self.public()
        self.assertNotIn('"onmouseover=', html)
        self.assertNotIn("<script>alert(2)", html)
        self.assertNotIn('href="javascript', html)

    def test_plain_comments_show_at_once_and_existing_ones_stay_approved(self):
        self.c.post("/stories/hello/comment", headers=ORIGIN, data={"content": "Asante sana"})
        self.assertIn("Asante sana", self.public())
        cid = self.comment("An older comment")                       # no approved value given
        with app.app_context():
            c = db.session.get(Comment, cid)
            self.assertTrue(c.approved)
            self.assertFalse(c.link_trusted)

    def test_delete(self):
        cid = self.comment("Delete me please", approved=True)
        self.login()
        self.assertIn("Delete this comment for good?", self.c.get("/admin/comments?tab=all").get_data(as_text=True))
        r = self.c.post("/admin/comments/delete/%d" % cid, headers=ORIGIN,
                        data={"csrf_token": self.token(), "tab": "all", "page": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/admin/comments?tab=all"))
        with app.app_context():
            self.assertIsNone(db.session.get(Comment, cid))
        self.assertNotIn("Delete me please", self.public())
        r = self.c.post("/admin/comments/delete/%d" % cid, headers=ORIGIN, data={"csrf_token": self.token()})
        self.assertEqual(r.status_code, 404)

    def test_tabs_counts_and_dashboard_link(self):
        self.comment("held one https://a.example", approved=False)
        self.comment("held two www.b.example", approved=False)
        self.comment("fine", approved=True)
        self.login()
        html = self.c.get("/admin/comments").get_data(as_text=True)      # Held is the default tab
        self.assertRegex(html, r'aria-current="page">\s*Held <span class="tab-count">2</span>')
        self.assertIn('Approved <span class="tab-count">1</span>', html)
        self.assertIn('All <span class="tab-count">3</span>', html)
        self.assertNotIn(">fine<", html)
        self.assertIn(">fine<", self.c.get("/admin/comments?tab=all").get_data(as_text=True))
        self.assertIn("No held comments", self.c.get("/admin/comments?tab=nonsense&page=9").get_data(as_text=True))
        dash = self.c.get("/admin").get_data(as_text=True)
        self.assertIn("Review 2 held comments", dash)
        self.assertIn('href="/admin/comments?tab=held"', dash)
        # the sidebar links to it on every admin page
        for url in ("/admin", "/admin/stories", "/admin/create-story", "/admin/submissions", "/admin/analytics"):
            self.assertIn('href="/admin/comments"', self.c.get(url).get_data(as_text=True), url)

    def test_comments_page_needs_login(self):
        r = self.c.get("/admin/comments")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.headers["Location"])


class FormTokenTests(Step3Base):
    def setUp(self):
        super().setUp()
        self.add_story()
        self.cid = self.comment("held https://x.example", approved=False)

    def state(self):
        with app.app_context():
            c = db.session.get(Comment, self.cid)
            return None if c is None else (c.approved, c.link_trusted)

    def test_token_is_required_for_every_comment_action(self):
        self.login()
        good = self.token()
        for path in ("/admin/comments/%d/approve", "/admin/comments/%d/approve-trust",
                     "/admin/comments/delete/%d"):
            url = path % self.cid
            for data in ({}, {"csrf_token": ""}, {"csrf_token": "wrong"}, {"csrf_token": good + "x"}):
                self.assertEqual(self.c.post(url, headers=ORIGIN, data=data).status_code, 403, (url, data))
            # the right token from another site is still refused (Origin check stays)
            r = self.c.post(url, headers={"Origin": "https://evil.example"}, data={"csrf_token": good})
            self.assertEqual(r.status_code, 403, url)
        self.assertEqual(self.state(), (False, False))                # nothing changed

    def test_token_is_tied_to_the_session(self):
        self.login()
        good = self.token()
        other = app.test_client()
        other.post("/admin/login", headers=ORIGIN, data={"username": "admin", "password": "admin123"})
        r = other.post("/admin/comments/%d/approve" % self.cid, headers=ORIGIN, data={"csrf_token": good})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.state(), (False, False))

    def test_not_logged_in_goes_to_login(self):
        r = self.c.post("/admin/comments/%d/approve" % self.cid, headers=ORIGIN, data={"csrf_token": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login", r.headers["Location"])
        self.assertEqual(self.state(), (False, False))

    def test_every_moderation_form_carries_the_token(self):
        self.login()
        html = self.c.get("/admin/comments?tab=all").get_data(as_text=True)
        forms = re.findall(r"<form[^>]*>.*?</form>", html, re.S)
        self.assertEqual(len(forms), 3)                                 # approve, trust, delete
        for f in forms:
            self.assertIn('name="csrf_token"', f)


# ───────────────────────── publish date ─────────────────────────

class PublishDateTests(Step3Base):
    def test_approval_sets_the_publish_date_and_keeps_the_submission_date(self):
        submitted = datetime.utcnow() - timedelta(days=10)
        self.add_story(title="Pending", slug="pending", published=False, created_at=submitted)
        with app.app_context():
            sid = Story.query.filter_by(slug="pending").one().id
            self.assertIsNone(db.session.get(Story, sid).published_at)
        self.login()
        before = datetime.utcnow()
        r = self.c.post("/admin/stories/approve/%d" % sid, headers=ORIGIN, data={"csrf_token": self.token()})
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            s = db.session.get(Story, sid)
            self.assertTrue(s.published)
            self.assertGreaterEqual(s.published_at, before - timedelta(seconds=1))
            self.assertEqual(s.created_at, submitted)                   # submission date kept

    def test_create_and_publish_toggle_set_the_date(self):
        self.login()
        t = self.token()
        self.c.post("/admin/create-story", headers=ORIGIN,
                    data={"title": "Now", "content": "Text", "published": "on", "csrf_token": t})
        self.c.post("/admin/create-story", headers=ORIGIN, data={"title": "Later", "content": "Text", "csrf_token": t})
        with app.app_context():
            self.assertIsNotNone(Story.query.filter_by(slug="now").one().published_at)
            later = Story.query.filter_by(slug="later").one()
            self.assertIsNone(later.published_at)
            lid = later.id
        self.c.post("/admin/stories/toggle/%d" % lid, headers=ORIGIN, data={"csrf_token": t})  # publish
        with app.app_context():
            first = db.session.get(Story, lid).published_at
            self.assertIsNotNone(first)
        self.c.post("/admin/stories/toggle/%d" % lid, headers=ORIGIN, data={"csrf_token": t})  # unpublish keeps the date
        with app.app_context():
            s = db.session.get(Story, lid)
            self.assertFalse(s.published)
            self.assertEqual(s.published_at, first)
        self.c.post("/admin/stories/toggle/%d" % lid, headers=ORIGIN, data={"csrf_token": t})  # re-publish keeps it too
        with app.app_context():
            s = db.session.get(Story, lid)
            self.assertTrue(s.published)
            self.assertEqual(s.published_at, first)

    def test_approving_a_story_that_was_published_before_keeps_its_date(self):
        first = datetime(2026, 1, 5, 9, 30)
        self.add_story(title="Back", slug="back", published=False, published_at=first,
                       created_at=datetime(2026, 1, 1))
        with app.app_context():
            sid = Story.query.filter_by(slug="back").one().id
        self.login()
        self.c.post("/admin/stories/approve/%d" % sid, headers=ORIGIN, data={"csrf_token": self.token()})
        with app.app_context():
            s = db.session.get(Story, sid)
            self.assertTrue(s.published)
            self.assertEqual(s.published_at, first)

    def make_order_case(self):
        now = datetime.utcnow()
        # written long ago but approved just now -> newest
        self.add_story(title="Old but new", slug="old-but-new", created_at=now - timedelta(days=30),
                       published_at=now - timedelta(minutes=5))
        # written and published yesterday
        self.add_story(title="Yesterday", slug="yesterday", created_at=now - timedelta(days=1),
                       published_at=now - timedelta(days=1))
        # an old row from before this change: no publish date, falls back to its submission date
        self.add_story(title="Legacy", slug="legacy", created_at=now - timedelta(days=3), published_at=None)
        return now

    def test_ordering_by_publish_date_everywhere(self):
        now = self.make_order_case()
        expected = ["old-but-new", "yesterday", "legacy"]

        html = self.c.get("/stories").get_data(as_text=True)
        found = re.findall(r'href="/stories/([a-z0-9-]+)" class="card-link"', html)
        self.assertEqual(found, expected)

        data = self.c.get("/api/stories").get_json()
        self.assertEqual([s["slug"] for s in data["stories"]], expected)
        first = data["stories"][0]
        self.assertTrue(first["published_at"].startswith((now - timedelta(minutes=5)).strftime("%Y-%m-%dT")))
        self.assertTrue(first["submitted_at"].startswith((now - timedelta(days=30)).strftime("%Y-%m-%d")))

        xml = self.c.get("/sitemap.xml").get_data(as_text=True)
        self.assertEqual(re.findall(r"/stories/([a-z0-9-]+)</loc>", xml), expected)

        self.login()
        admin = self.c.get("/admin/stories").get_data(as_text=True)
        pos = [admin.index("Old but new"), admin.index("Yesterday"), admin.index("Legacy")]
        self.assertEqual(pos, sorted(pos))

    def test_story_page_and_list_show_the_publish_date(self):
        now = self.make_order_case()
        shown = (now - timedelta(minutes=5)).strftime("%B %d, %Y")
        written = (now - timedelta(days=30)).strftime("%B %d, %Y")
        html = self.c.get("/stories/old-but-new").get_data(as_text=True)
        self.assertIn(shown, html)
        self.assertNotIn(written, html)
        lst = self.c.get("/stories").get_data(as_text=True)
        self.assertIn((now - timedelta(minutes=5)).strftime("%b %d, %Y"), lst)

    def test_old_rows_without_a_publish_date_still_show(self):
        old = datetime(2025, 3, 4, 12, 0)
        self.add_story(title="Very old", slug="very-old", created_at=old, published_at=None)
        self.assertIn("Mar 04, 2025", self.c.get("/stories").get_data(as_text=True))
        self.assertIn("March 04, 2025", self.c.get("/stories/very-old").get_data(as_text=True))
        self.assertEqual(self.c.get("/api/stories").get_json()["stories"][0]["published_at"],
                         "2025-03-04T12:00:00Z")
        self.assertIn("very-old", self.c.get("/sitemap.xml").get_data(as_text=True))

    def test_upgrade_adds_the_columns_and_fills_old_rows(self):
        old = datetime(2025, 3, 4, 12, 0)
        self.add_story(title="Very old", slug="very-old", created_at=old, published_at=None)
        self.add_story(title="Waiting", slug="waiting", created_at=old, published=False)
        self.comment("old comment", slug="very-old")
        with app.app_context():
            # make the tables look like the database before this change
            for sql in ("DROP INDEX IF EXISTS ix_stories_pub_published",
                        "DROP INDEX IF EXISTS ix_comments_held",
                        "ALTER TABLE stories DROP COLUMN published_at",
                        "ALTER TABLE comments DROP COLUMN link_trusted"):
                db.session.execute(db.text(sql))
            db.session.commit()
            app_module.upgrade_schema()
            app_module.upgrade_schema()                                   # twice is harmless
            rows = dict(db.session.execute(db.text("SELECT slug, published_at FROM stories")).all())
            self.assertTrue(str(rows["very-old"]).startswith("2025-03-04 12:00"))
            self.assertIsNone(rows["waiting"])                            # pending: no publish date
            trusted = db.session.execute(db.text("SELECT approved, link_trusted FROM comments")).one()
            self.assertEqual((bool(trusted[0]), bool(trusted[1])), (True, False))
        self.assertIn("old comment", self.public("very-old"))



# ───────────────────────── form token on the story actions ─────────────────────────

class StoryFormTokenTests(Step3Base):
    def setUp(self):
        super().setUp()
        self.add_story(title="Live", slug="live")
        self.add_story(title="Pending", slug="pending", published=False)
        with app.app_context():
            self.live = Story.query.filter_by(slug="live").one().id
            self.pending = Story.query.filter_by(slug="pending").one().id
        self.login()

    def snapshot(self):
        with app.app_context():
            return sorted((s.slug, s.title, bool(s.published)) for s in Story.query.all())

    def actions(self):
        return [
            ("/admin/create-story", {"title": "Sneaky", "content": "x", "published": "on"}),
            ("/admin/stories/approve/%d" % self.pending, {}),
            ("/admin/stories/edit/%d" % self.live, {"title": "Changed", "content": "y"}),
            ("/admin/stories/toggle/%d" % self.live, {}),
            ("/admin/stories/delete/%d" % self.live, {}),
        ]

    def test_every_story_action_needs_the_token(self):
        before = self.snapshot()
        good = self.token()
        for url, data in self.actions():
            for token in (None, "", "wrong", good + "x"):
                d = dict(data)
                if token is not None:
                    d["csrf_token"] = token
                self.assertEqual(self.c.post(url, headers=ORIGIN, data=d).status_code, 403, (url, token))
            # right token, wrong site: the Origin check still refuses it
            d = dict(data, csrf_token=good)
            self.assertEqual(self.c.post(url, headers={"Origin": "https://evil.example"}, data=d).status_code, 403, url)
        self.assertEqual(self.snapshot(), before)                     # nothing changed

    def test_every_story_action_works_with_the_token(self):
        t = self.token()
        expect = [302, 302, 302, 302, 302]
        for (url, data), code in zip(self.actions(), expect):
            self.assertEqual(self.c.post(url, headers=ORIGIN, data=dict(data, csrf_token=t)).status_code, code, url)
        with app.app_context():
            self.assertIsNone(db.session.get(Story, self.live))       # deleted last
            self.assertTrue(db.session.get(Story, self.pending).published)
            self.assertTrue(Story.query.filter_by(title="Sneaky").one().published)

    def test_every_admin_post_form_carries_the_token(self):
        pages = ["/admin/create-story", "/admin/stories/edit/%d" % self.live,
                 "/admin/stories/review/%d" % self.pending, "/admin/stories", "/admin/submissions"]
        seen = 0
        for url in pages:
            html = self.c.get(url).get_data(as_text=True)
            for form in re.findall(r"<form\b.*?</form>", html, re.S):
                if re.search(r'method="post"', form, re.I):
                    seen += 1
                    self.assertIn('name="csrf_token"', form, url)
        self.assertGreaterEqual(seen, 7)


# ───────────────────────── safe schema upgrade ─────────────────────────

class SchemaUpgradeTests(Step3Base):
    def run_upgrade(self):
        with app.app_context():
            with self.assertLogs("stories.schema", level="INFO") as logs:
                report = app_module.upgrade_schema()
        return report, "\n".join(logs.output)

    def test_database_that_already_has_everything(self):
        report, log = self.run_upgrade()                              # fresh tables from create_all
        self.assertEqual(report["added"], [])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["indexes"], len(app_module.INDEXES))
        self.assertIn("[schema] upgrade done (sqlite): added=none", log)
        self.assertIn("warnings=0", log)
        self.assertNotIn("WARNING", log)

    def test_running_twice_is_harmless(self):
        with app.app_context():
            db.session.execute(db.text("DROP INDEX IF EXISTS ix_stories_pub_published"))
            db.session.execute(db.text("ALTER TABLE stories DROP COLUMN published_at"))
            db.session.commit()
        first, log1 = self.run_upgrade()
        second, log2 = self.run_upgrade()
        self.assertEqual(first["added"], ["stories.published_at"])
        self.assertIn("[schema] added column stories.published_at", log1)
        self.assertEqual(second["added"], [])
        self.assertEqual(first["warnings"] + second["warnings"], [])
        self.assertIn("added=none", log2)

    def test_a_failing_step_is_logged_and_the_rest_still_runs(self):
        with app.app_context():
            for sql in ("DROP INDEX IF EXISTS ix_stories_pub_published", "DROP INDEX IF EXISTS ix_comments_held",
                        "ALTER TABLE stories DROP COLUMN published_at",
                        "ALTER TABLE comments DROP COLUMN link_trusted"):
                db.session.execute(db.text(sql))
            db.session.commit()

            real = db.session.execute

            def flaky(statement, *a, **k):
                if "ADD COLUMN published_at" in str(statement):
                    raise RuntimeError("simulated failure")
                return real(statement, *a, **k)

            with mock.patch.object(db.session, "execute", side_effect=flaky):
                with self.assertLogs("stories.schema", level="INFO") as logs:
                    report = app_module.upgrade_schema()          # must not raise
            log = "\n".join(logs.output)
            self.assertIn("comments.link_trusted", report["added"])   # the next column still added
            self.assertNotIn("stories.published_at", report["added"])
            self.assertTrue(any("add column stories.published_at failed: RuntimeError" in w
                                for w in report["warnings"]))
            self.assertIn("WARNING:stories.schema:[schema] add column stories.published_at failed", log)
            # the next start (no failure) finishes the job
            report2 = app_module.upgrade_schema()
            self.assertEqual(report2["added"], ["stories.published_at"])
            self.assertEqual(report2["warnings"], [])

    def test_unreadable_database_does_not_raise(self):
        with app.app_context():
            with mock.patch.object(app_module.db, "inspect", side_effect=RuntimeError("down")):
                with self.assertLogs("stories.schema", level="WARNING"):
                    report = app_module.upgrade_schema()
        self.assertTrue(report["warnings"])


if __name__ == "__main__":
    unittest.main()
