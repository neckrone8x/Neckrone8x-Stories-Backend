"""Step 5: reader tools. The saved-stories endpoint and page, related stories,
and the markup the browser script needs (bookmarks, continue reading, reading
settings, share). The browser side itself is checked separately in Node."""
import os
import re
import unittest
from datetime import datetime, timedelta

from test_app_step1 import HOST, TOKEN, AppBase, Story, app, db  # sets up a temp DB

import site_security as S
from app import Reaction

DATA = "/stories/saved/data"


class Step5Base(AppBase):
    def story(self, slug, **kw):
        kw.setdefault("title", slug.replace("-", " ").title())
        self.add_story(slug=slug, **kw)

    def react(self, slug, n):
        with app.app_context():
            sid = Story.query.filter_by(slug=slug).one().id
            for i in range(n):
                db.session.add(Reaction(story_id=sid, reaction_type="love", ip_address="v-%s-%d" % (slug, i)))
            db.session.commit()

    def data(self, slugs, **headers):
        r = self.c.get(DATA, query_string={"slugs": ",".join(slugs)}, headers=headers)
        return r, (r.get_json() or {}).get("stories")

    def html(self, url):
        return self.c.get(url).get_data(as_text=True)


# ───────────────────────── saved-stories endpoint ─────────────────────────

class SavedDataTests(Step5Base):
    def test_only_published_in_the_order_asked_unknown_ignored(self):
        now = datetime.utcnow()
        self.story("one", created_at=now)
        self.story("two", created_at=now - timedelta(days=1))
        self.story("hidden", published=False)
        r, stories = self.data(["two", "nope", "hidden", "one", "two"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual([s["slug"] for s in stories], ["two", "one"])
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        item = stories[0]
        self.assertEqual(set(item), {"slug", "title", "author", "category", "adult", "excerpt",
                                     "reading_minutes", "read_count", "published_at",
                                     "published_label", "url"})
        self.assertEqual(item["url"], "/stories/two")
        self.assertFalse(item["adult"])
        self.assertTrue(item["excerpt"].startswith("Some text"))

    def test_bad_or_empty_input_is_harmless(self):
        self.story("one")
        for raw in ("", ",,,", "ONE", " one ", "../admin", "a b", "<x>", "one'--", "-x"):
            r = self.c.get(DATA, query_string={"slugs": raw})
            self.assertEqual(r.status_code, 200, raw)
            got = [s["slug"] for s in r.get_json()["stories"]]
            self.assertEqual(got, ["one"] if raw in ("ONE", " one ") else [], raw)
        self.assertEqual(self.c.get(DATA).get_json(), {"stories": []})

    def test_capped_at_fifty(self):
        with app.app_context():
            for i in range(60):
                db.session.add(Story(title="S%d" % i, slug="s%d" % i, category="Life",
                                     content="x " * 50, author="A", published=True,
                                     created_at=datetime.utcnow()))
            db.session.commit()
        _, stories = self.data(["s%d" % i for i in range(60)])
        self.assertEqual(len(stories), 50)
        self.assertEqual(stories[-1]["slug"], "s49")
        # unknown slugs still count towards the 50, so a long junk list cannot widen it
        _, stories = self.data(["junk-%d" % i for i in range(50)] + ["s1"])
        self.assertEqual(stories, [])

    def test_adult_stories_listed_without_their_text(self):
        self.story("grown", category="18+", content="SECRET ADULT WORDS " * 30)
        r, stories = self.data(["grown"])
        self.assertEqual(len(stories), 1)
        self.assertTrue(stories[0]["adult"])
        self.assertIsNone(stories[0]["excerpt"])
        self.assertNotIn("SECRET", r.get_data(as_text=True))

    def test_open_to_browsers_even_when_the_feed_token_is_set(self):
        os.environ["FEED_TOKEN"] = TOKEN
        self.story("one")
        r, stories = self.data(["one"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(stories), 1)

    def test_rate_limited_with_retry_after(self):
        for _ in range(60):
            self.c.get(DATA)
        r = self.c.get(DATA)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)


# ───────────────────────── saved page + header ─────────────────────────

class SavedPageTests(Step5Base):
    def test_saved_page_renders_with_every_state_and_is_not_indexed(self):
        r = self.c.get("/stories/saved")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-Robots-Tag"], "noindex")
        html = r.get_data(as_text=True)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn('<meta name="robots" content="noindex">', html)
        self.assertIn('<h1 id="saved-title"', html)
        for part in ('id="savedLoading"', 'id="savedEmpty" hidden', 'id="savedBlocked" hidden',
                     'id="savedError" hidden', 'data-endpoint="/stories/saved/data" hidden',
                     '<template id="savedTemplate">', "<noscript>", "saved-remove"):
            self.assertIn(part, html)
        self.assertRegex(html, r'href="/stories/saved" class="header-saved" aria-current="page"')

    def test_header_has_the_saved_link_everywhere(self):
        self.story("hello-there")
        for url in ("/stories", "/stories/hello-there", "/stories/submit"):
            html = self.html(url)
            self.assertRegex(html, r'<a href="/stories/saved" class="header-saved">\s*<svg[^>]*aria-hidden="true"', url)
            self.assertIn('<span class="header-saved-text">Saved</span>', html)

    def test_a_story_titled_saved_does_not_take_the_page(self):
        self.assertIn("saved", S.RESERVED_SLUGS)
        self.assertEqual(S.make_slug("Saved", lambda s: False), "saved-story")


# ───────────────────────── related stories ─────────────────────────

class RelatedTests(Step5Base):
    def related(self, html):
        m = re.search(r'<section class="panel related".*?</section>', html, re.S)
        return re.findall(r'href="/stories/([a-z0-9-]+)" class="card-link"', m.group(0)) if m else None

    def test_same_category_most_loved_then_newest_max_three_never_itself(self):
        now = datetime.utcnow()
        self.story("main", category="Life", created_at=now)
        self.story("old-loved", category="Life", created_at=now - timedelta(days=9))
        self.story("new-a", category="life", created_at=now - timedelta(days=1))   # case does not matter
        self.story("new-b", category="Life", created_at=now - timedelta(days=2))
        self.story("new-c", category="Life", created_at=now - timedelta(days=3))
        self.story("draft", category="Life", published=False)
        self.story("other", category="News")
        self.react("old-loved", 3)
        self.react("main", 9)
        html = self.html("/stories/main")
        self.assertEqual(self.related(html), ["old-loved", "new-a", "new-b"])
        self.assertIn("More Life stories", html)
        self.assertLess(html.index('id="comments"'), html.index('class="panel related"'))

    def test_hidden_when_none(self):
        self.story("alone", category="Novels")
        self.story("elsewhere", category="News")
        html = self.html("/stories/alone")
        self.assertIsNone(self.related(html))
        self.assertNotIn("More Novels stories", html)

    def test_no_adult_stories_under_other_stories(self):
        self.story("main", category="Life")
        self.story("grown", category="18+")
        self.assertIsNone(self.related(self.html("/stories/main")))

    def test_adult_related_only_after_the_age_gate(self):
        self.story("grown-a", category="18+", title="Grown A", content="ADULT TEXT A " * 30)
        self.story("grown-b", category="18+", title="Grown B", content="ADULT TEXT B " * 30)
        gate = self.html("/stories/grown-a")
        self.assertIsNone(self.related(gate))                 # the gate page lists nothing
        self.assertNotIn("Grown B", gate)
        self.c.get("/stories/grown-a/verify-age")
        html = self.html("/stories/grown-a")
        self.assertEqual(self.related(html), ["grown-b"])
        self.assertNotIn("ADULT TEXT B", html)                # title only, no text
        self.assertIn("tag-adult", re.search(r'<section class="panel related".*?</section>', html, re.S).group(0))
        # the related story still has its own gate
        self.assertNotIn("ADULT TEXT B", self.html("/stories/grown-b"))


# ───────────────────────── markup the browser script needs ─────────────────────────

class ReaderToolMarkupTests(Step5Base):
    def test_story_tools_start_hidden_and_are_labelled(self):
        self.story("hello")
        html = self.html("/stories/hello")
        self.assertIn('<body class="is-story">', html)
        self.assertIn('id="story" data-slug="hello"', html)
        self.assertRegex(html, r'class="tool-button bookmark-button" data-bookmark="hello"\s+aria-pressed="false" hidden>')
        self.assertRegex(html, r'id="readingToggle"\s+aria-expanded="false" aria-controls="readingPanel" hidden>')
        self.assertIn('<span class="visually-hidden"> and background settings</span>', html)
        self.assertIn('id="readingPanel" role="group" aria-labelledby="reading-title" hidden', html)
        self.assertIn("<legend>Text size</legend>", html)
        for value in ("s", "m", "l", "xl"):
            self.assertIn('<label class="size-option" for="text-size-%s">' % value, html)
        self.assertIn('id="text-size-m" name="text-size" value="m" checked', html)
        self.assertIn('<label class="sepia-option" for="sepiaToggle">', html)
        # tools come after the top reactions, before the story text
        self.assertLess(html.index('class="reactions-top"'), html.index('class="story-tools"'))
        self.assertLess(html.index('class="story-tools"'), html.index('class="story-body"'))

    def test_share_sheet_button_and_clean_link(self):
        self.story("hello")
        html = self.html("/stories/hello?utm_source=whatsapp&fbclid=abc")
        self.assertIn('data-share-url="http://localhost/stories/hello"', html)
        self.assertIn('id="shareNative" hidden', html)
        self.assertEqual(html.count('class="share-button share-alt"'), 4)    # WhatsApp, Facebook, X, copy
        self.assertIn('data-url="http://localhost/stories/hello"', html)
        self.assertNotIn("utm_source", html)
        self.assertNotIn("fbclid", html)

    def test_cards_have_hidden_labelled_save_buttons(self):
        now = datetime.utcnow()
        self.story("one", title="First", created_at=now)
        self.story("two", title="Second", created_at=now - timedelta(days=1))
        html = self.html("/stories")
        for slug, title in (("one", "First"), ("two", "Second")):
            self.assertRegex(html, r'class="card-save" data-bookmark="%s" aria-pressed="false"\s+'
                                   r'aria-label="Save “%s” for later" hidden>' % (slug, title))

    def test_continue_row_only_on_the_plain_first_page(self):
        self.story("one")
        html = self.html("/stories")
        self.assertIn('id="continueReading" aria-labelledby="continue-title"', html)
        self.assertRegex(html, r'data-endpoint="/stories/saved/data" hidden>')
        self.assertIn('<template id="continueTemplate">', html)
        self.assertIn('id="continueClear"', html)
        self.assertLess(html.index('id="continueReading"'), html.index('class="listing"'))
        for url in ("/stories?search=one", "/stories?category=Life", "/stories?page=2"):
            self.assertNotIn('id="continueReading"', self.html(url), url)

    def test_hidden_beats_class_display_rules(self):
        """The tools start with the hidden attribute; classes such as
        .share-button set display, so one global rule must keep hidden hidden."""
        css = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "static", "css", "stories.css"), encoding="utf-8").read()
        self.assertIn("[hidden] { display: none !important; }", css)

    def test_reading_settings_applied_before_drawing(self):
        html = self.html("/stories")
        head = html[:html.index("</head>")]
        self.assertIn("localStorage.getItem('n8x.reading')", head)
        self.assertIn("data-text-size", head)
        self.assertIn("data-sepia", head)


if __name__ == "__main__":
    unittest.main()
