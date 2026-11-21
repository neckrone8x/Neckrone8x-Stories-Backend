"""Tests for the shared layout (step 2): every public page renders on
base.html, the theme script runs before the CSS, every input has a label,
no emoji icons in the header, and all features still work."""
import os
import re
import unittest
from datetime import datetime, timedelta
from html.parser import HTMLParser

from test_app_step1 import HOST, AppBase, Story, app, db  # sets up a temp DB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿\U0001F000-\U0001F2FF️]")


class Tags(HTMLParser):
    """Collects tags with their attributes (and the text inside labels)."""

    def __init__(self):
        super().__init__()
        self.tags, self.label_for = [], set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])


def parse(html):
    p = Tags()
    p.feed(html)
    return p


def head_of(html):
    return html[:html.index("</head>")]


def header_of(html):
    m = re.search(r'<header class="site-header">.*?</header>', html, re.S)
    return m.group(0) if m else ""


class LayoutBase(AppBase):
    def setUp(self):
        super().setUp()
        self.add_story(title="Hello", slug="hello", content="First line.\n\n" + "Some text " * 300)
        self.add_story(title="Adult", slug="adult", category="18+", content="x " * 50)

    def pages(self):
        """Every public page, as (name, html)."""
        out = [
            ("list", self.c.get("/stories")),
            ("story", self.c.get("/stories/hello")),
            ("submit", self.c.get("/stories/submit")),
            ("thanks", self.c.post("/stories/submit", headers={"Origin": HOST},
                                   data={"title": "T", "content": "C"})),
            ("login", self.c.get("/admin/login")),
            ("gate", self.c.get("/stories/adult")),
            ("404", self.c.get("/stories/nope")),
        ]
        return [(n, r.status_code, r.get_data(as_text=True)) for n, r in out]


class SharedLayoutTests(LayoutBase):
    def test_every_public_page_renders_on_the_shared_layout(self):
        for name, status, html in self.pages():
            self.assertEqual(status, 404 if name == "404" else 200, name)
            self.assertTrue(html.startswith("<!DOCTYPE html>"), name)  # no stray BOM/text first
            self.assertIn('<header class="site-header">', html, name)
            self.assertIn('<footer class="site-footer">', html, name)
            self.assertIn('href="#main" class="skip-link"', html, name)
            self.assertIn('<main id="main"', html, name)
            self.assertIn("css/stories.css", html, name)
            self.assertEqual(html.count("<html"), 1, name)
            self.assertIn("© %d" % datetime.utcnow().year, html, name)

    def test_theme_script_runs_in_the_head_before_the_stylesheet(self):
        for name, _, html in self.pages():
            head = head_of(html)
            self.assertIn("localStorage.getItem('theme')", head, name)
            self.assertIn("prefers-color-scheme: light", head, name)
            self.assertIn("try {", head, name)
            self.assertLess(head.index("localStorage.getItem('theme')"), head.index("css/stories.css"), name)
            self.assertIn('id="themeToggle"', html, name)
            self.assertRegex(html, r'id="themeToggle" aria-label="Switch to (light|dark) theme"', name)

    def test_fonts_are_linked_not_imported(self):
        css = open(os.path.join(ROOT, "static", "css", "stories.css"), encoding="utf-8").read()
        self.assertNotIn("@import", css)
        self.assertIn('[data-theme="light"]', css)
        self.assertIn("Source Serif 4", css)
        self.assertIn("Inter", css)
        head = head_of(self.c.get("/stories").get_data(as_text=True))
        self.assertIn('rel="preconnect" href="https://fonts.googleapis.com"', head)
        self.assertIn('rel="preconnect" href="https://fonts.gstatic.com" crossorigin', head)
        self.assertRegex(head, r'fonts\.googleapis\.com/css2\?family=Inter[^"]*Source\+Serif\+4[^"]*display=swap')

    def test_no_emoji_icons_in_the_header_or_list(self):
        for name, _, html in self.pages():
            header = header_of(html)
            self.assertTrue(header, name)
            self.assertIsNone(EMOJI.search(header), name)
        self.assertIsNone(EMOJI.search(self.c.get("/stories").get_data(as_text=True)))

    def test_every_input_has_a_label(self):
        for name, _, html in self.pages():
            p = parse(html)
            for tag, a in p.tags:
                if tag not in ("input", "select", "textarea"):
                    continue
                if a.get("type") in ("hidden", "submit", "button"):
                    continue
                self.assertTrue(a.get("id"), (name, tag, a))
                self.assertIn(a["id"], p.label_for, (name, tag, a))

    def test_icons_are_hidden_and_icon_buttons_have_a_name(self):
        for name, _, html in self.pages():
            for tag, a in parse(html).tags:
                if tag == "svg" and "icon" in a.get("class", ""):
                    self.assertEqual(a.get("aria-hidden"), "true", name)
            # the theme toggle has only icons inside, so it must have an aria-label
            self.assertRegex(html, r'<button type="button" class="theme-toggle" id="themeToggle" aria-label="[^"]+"')

    def test_thank_you_page_is_a_template_not_raw_html_in_app_py(self):
        src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        self.assertNotIn("<!DOCTYPE", src)
        self.assertNotIn("<style>", src)
        html = dict((n, h) for n, _, h in self.pages())["thanks"]
        self.assertIn("Story submitted", html)
        with app.app_context():
            self.assertFalse(Story.query.filter_by(title="T").one().published)

    def test_no_inline_style_blocks_in_page_templates(self):
        for folder in ("stories", "admin"):
            path = os.path.join(ROOT, "templates", folder)
            for f in os.listdir(path):
                if f.endswith(".html"):
                    with open(os.path.join(path, f), encoding="utf-8") as fh:
                        self.assertNotIn("<style", fh.read(), f)


class ListPageTests(LayoutBase):
    def test_search_has_a_label_and_role(self):
        html = self.c.get("/stories").get_data(as_text=True)
        self.assertIn('<label for="search"', html)
        self.assertIn('role="search"', html)
        self.assertIn('type="search" id="search" name="search"', html)

    def test_cards_show_category_reading_time_reads_and_date(self):
        html = self.c.get("/stories").get_data(as_text=True)
        self.assertIn("card-featured", html)          # first story on page 1
        self.assertIn("2 stories", html)
        self.assertIn("3 min read", html)             # 602 words / 200
        self.assertIn("0 reads", html)
        self.assertIn(datetime.utcnow().strftime("%b %d, %Y"), html)
        self.assertIn("Adult story. You will be asked to confirm your age", html)
        self.assertNotIn("x x x x", html)             # no 18+ text on the list

    def test_category_chip_marks_the_current_one(self):
        html = self.c.get("/stories?category=life").get_data(as_text=True)
        self.assertRegex(html, r'class="chip" aria-current="page">Life<')
        self.assertNotIn("card-featured", html)       # no featured story when filtered
        self.assertIn("Life stories", html)

    def test_no_results_and_empty_states(self):
        html = self.c.get("/stories?search=zzzz").get_data(as_text=True)
        self.assertIn("No stories found", html)
        self.assertIn("Nothing matches", html)
        self.assertIn("zzzz", html)
        with app.app_context():
            db.session.query(Story).delete()
            db.session.commit()
        html = self.c.get("/stories").get_data(as_text=True)
        self.assertIn("No stories yet", html)
        self.assertIn("0 stories", html)

    def test_search_and_pagination_still_work(self):
        now = datetime.utcnow()
        for i in range(8):
            self.add_story(title="Mango %d" % i, slug="mango-%d" % i, created_at=now + timedelta(minutes=i))
        html = self.c.get("/stories?search=mango").get_data(as_text=True)
        self.assertIn("8 stories", html)
        self.assertIn('aria-label="Pages"', html)
        self.assertIn('rel="next"', html)
        self.assertNotIn("card-featured", html)
        page2 = self.c.get("/stories?search=mango&page=2").get_data(as_text=True)
        self.assertIn("Mango 0", page2)
        self.assertIn('rel="prev"', page2)

    def test_list_page_search_is_escaped(self):
        html = self.c.get("/stories?search=<script>alert(1)</script>").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)


class FeaturesStillWorkTests(LayoutBase):
    def test_story_page_keeps_reactions_comments_share_and_progress(self):
        html = self.c.get("/stories/hello").get_data(as_text=True)
        for needle in ('id="progressBar"', 'id="story"', 'id="reactions"', 'id="comments"',
                       'id="copyLink"', "wa.me", 'aria-pressed="false"', 'class="story-body"',
                       '<label for="comment-name"', '<label for="comment-content"'):
            self.assertIn(needle, html)
        r = self.c.post("/stories/hello/react/love", headers={"Origin": HOST})
        self.assertEqual(r.status_code, 302)
        self.assertIn('aria-pressed="true"', self.c.get("/stories/hello").get_data(as_text=True))
        r = self.c.post("/stories/hello/comment", headers={"Origin": HOST}, data={"name": "Amina", "content": "Nice one"})
        self.assertEqual(r.status_code, 302)
        html = self.c.get("/stories/hello").get_data(as_text=True)
        self.assertIn("Nice one", html)
        self.assertIn("Comments <span class=\"muted\">(1)</span>", html)

    def test_age_gate_still_guards_18_plus(self):
        html = self.c.get("/stories/adult").get_data(as_text=True)
        self.assertIn("18+ content", html)
        self.assertNotIn("x x x", html)
        self.assertNotIn("mkuu-helper.js", html)
        self.c.get("/stories/adult/verify-age")
        html = self.c.get("/stories/adult").get_data(as_text=True)
        self.assertIn('class="story-body"', html)
        self.assertNotIn("mkuu-helper.js", html)  # still no bubble on an 18+ story

    def test_mkuu_bubble_on_public_pages_only(self):
        for url in ("/stories", "/stories/hello", "/stories/submit"):
            self.assertIn("mkuu-helper.js", self.c.get(url).get_data(as_text=True), url)
        self.assertIn('data-story="hello"', self.c.get("/stories/hello").get_data(as_text=True))
        self.assertNotIn("mkuu-helper.js", self.c.get("/admin/login").get_data(as_text=True))
        css = open(os.path.join(ROOT, "static", "css", "stories.css"), encoding="utf-8").read()
        self.assertIn('[data-theme="light"] .mkh-root', css)

    def test_submit_errors_show_and_keep_what_was_typed(self):
        r = self.c.post("/stories/submit", headers={"Origin": HOST}, data={"title": "Only a title", "content": ""})
        html = r.get_data(as_text=True)
        self.assertIn('role="alert"', html)
        self.assertIn('value="Only a title"', html)

    def test_admin_login_and_edit_page(self):
        r = self.c.post("/admin/login", headers={"Origin": HOST}, data={"username": "x", "password": "y"})
        self.assertIn('role="alert"', r.get_data(as_text=True))
        r = self.c.post("/admin/login", headers={"Origin": HOST}, data={"username": "admin", "password": "admin123"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/admin").status_code, 200)
        with app.app_context():
            sid = Story.query.filter_by(slug="hello").one().id
        html = self.c.get("/admin/stories/edit/%d" % sid).get_data(as_text=True)
        self.assertIn("css/admin.css", html)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        self.assertIn('value="Hello"', html)
        self.assertRegex(html, r'<option value="Life" selected>')
        r = self.c.post("/admin/stories/edit/%d" % sid, headers={"Origin": HOST},
                        data={"title": "Hello again", "category": "Life", "content": "New text", "author": "B",
                              "csrf_token": token})
        self.assertEqual(r.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Story, sid).title, "Hello again")


if __name__ == "__main__":
    unittest.main()
