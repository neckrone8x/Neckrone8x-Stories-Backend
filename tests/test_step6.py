"""Step 6: one category list (categories.py) used everywhere, the chips with
counts and the More panel, tolerant ?category= matching, and unknown
categories from the URL, the API and Mkuu."""
import glob
import os
import re
import unittest
from datetime import datetime, timedelta

from test_app_step1 import HOST, TOKEN, SUBMISSION, AppBase, Story, app, db  # temp DB

import categories as C
import site_security as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGIN = {"Origin": HOST}
NEW = ["Campus", "Family", "Mystery & Horror", "Politics", "Sports", "Technology"]
OLD = ["General", "Confession", "Experience", "News", "Relationship", "Marriage",
       "Life", "Novels", "18+", "Other"]


def options(html, select_id="category"):
    m = re.search(r'<select id="%s"[^>]*>(.*?)</select>' % select_id, html, re.S)
    return [v.replace("&amp;", "&") for v in re.findall(r'<option value="([^"]*)"', m.group(1))]


class Step6Base(AppBase):
    def stories_in(self, spec):
        """spec: {category: number of published stories}"""
        now = datetime.utcnow()
        n = 0
        with app.app_context():
            for category, count in spec.items():
                for _ in range(count):
                    n += 1
                    db.session.add(Story(title="T%d" % n, slug="t%d" % n, category=category,
                                         content="word " * 60, author="A", published=True,
                                         created_at=now - timedelta(minutes=n)))
            db.session.commit()

    def html(self, url):
        return self.c.get(url).get_data(as_text=True)

    def chips(self, html):
        """(shown chip names, More panel names) on the list page."""
        nav = re.search(r'<nav class="chips".*?</nav>', html, re.S).group(0)
        more = re.search(r'<div class="chips-more-panel".*?</div>', nav, re.S)
        more_html = more.group(0) if more else ""
        shown_html = nav.replace(more_html, "")
        name = r'class="chip"[^>]*>\s*<span>([^<]+)</span>'
        unescape = lambda xs: [x.replace("&amp;", "&") for x in xs]
        return unescape(re.findall(name, shown_html)), unescape(re.findall(name, more_html))

    def login(self):
        r = self.c.post("/admin/login", headers=ORIGIN, data={"username": "admin", "password": "admin123"})
        self.assertEqual(r.status_code, 302)

    def token(self):
        html = self.html("/admin/create-story")
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


# ───────────────────────── the single list ─────────────────────────

class ListTests(unittest.TestCase):
    def test_one_list_with_old_and_new_categories(self):
        self.assertEqual(set(C.CATEGORIES), set(OLD + NEW))
        self.assertEqual(len(C.CATEGORIES), 16)
        self.assertEqual(C.CATEGORIES[-2:], ["18+", "Other"])
        for c in C.CATEGORY_LIST:
            self.assertRegex(c.slug, r"^[a-z0-9]+(-[a-z0-9]+)*$", c)
        self.assertEqual(len({c.slug for c in C.CATEGORY_LIST}), 16)
        self.assertEqual(C.category_slug("Mystery & Horror"), "mystery-horror")
        self.assertEqual(C.category_slug("18+"), "18-plus")
        self.assertEqual(C.ADULT_CATEGORY, "18+")

    def test_site_security_reads_the_same_list(self):
        self.assertIs(S.CATEGORIES, C.CATEGORIES)
        self.assertIs(S.clean_category, C.clean_category)

    def test_matching_is_forgiving(self):
        for raw in ("Mystery & Horror", "mystery-horror", "MYSTERY & HORROR", "mystery and horror",
                    "Mystery&Horror", " mystery_horror "):
            self.assertEqual(C.canonical_category(raw), "Mystery & Horror", raw)
        for raw in ("18+", "18-plus", "18 plus"):
            self.assertEqual(C.canonical_category(raw), "18+", raw)
        self.assertEqual(C.canonical_category("LIFE"), "Life")
        for raw in ("", "mystery", "horror", "18", "<script>", None, 5, ["Life"], "x" * 200):
            self.assertIsNone(C.canonical_category(raw), raw)
            self.assertEqual(C.clean_category(raw), "General", raw)

    def test_no_other_copy_of_the_list_in_code_or_templates(self):
        files = [os.path.join(ROOT, "app.py"), os.path.join(ROOT, "site_security.py")]
        files += glob.glob(os.path.join(ROOT, "templates", "**", "*.html"), recursive=True)
        for path in files:
            text = open(path, encoding="utf-8-sig").read()
            self.assertNotRegex(text, r"""['"]Confession['"]\s*,\s*['"]Experience['"]""", path)
            self.assertNotIn('<option value="Confession"', text, path)


# ───────────────────────── forms and admin ─────────────────────────

class FormTests(Step6Base):
    def test_share_your_story_form(self):
        self.assertEqual(options(self.html("/stories/submit")), C.CATEGORIES)

    def test_submitted_category_is_checked_against_the_list(self):
        for sent, stored in (("mystery-horror", "Mystery & Horror"), ("Sports", "Sports"), ("Nonsense", "General")):
            S.limiter._hits.clear()
            r = self.c.post("/stories/submit", headers=ORIGIN,
                            data={"title": "A title " + sent, "content": "word " * 60, "category": sent})
            self.assertIn(r.status_code, (200, 302))
            with app.app_context():
                self.assertEqual(Story.query.filter_by(title="A title " + sent).one().category, stored)

    def test_admin_create_and_edit_forms(self):
        self.login()
        self.assertEqual(options(self.html("/admin/create-story")), C.CATEGORIES)
        self.stories_in({"Life": 1})
        with app.app_context():
            sid = Story.query.one().id
        self.assertEqual(options(self.html("/admin/stories/edit/%d" % sid)), C.CATEGORIES)

    def test_edit_can_move_a_story_to_every_category(self):
        self.login()
        self.stories_in({"Life": 1})
        with app.app_context():
            sid = Story.query.one().id
        for name in C.CATEGORIES:
            r = self.c.post("/admin/stories/edit/%d" % sid, headers=ORIGIN, data={
                "csrf_token": self.token(), "title": "T", "content": "word " * 60,
                "author": "A", "category": name})
            self.assertEqual(r.status_code, 302, name)
            with app.app_context():
                self.assertEqual(db.session.get(Story, sid).category, name)
            self.assertRegex(self.html("/admin/stories/edit/%d" % sid),
                             r'<option value="%s" selected>' % re.escape(name.replace("&", "&amp;")))

    def test_an_old_category_not_on_the_list_is_kept_until_changed(self):
        self.login()
        self.stories_in({"Love Letters": 1})
        with app.app_context():
            sid = Story.query.one().id
        html = self.html("/admin/stories/edit/%d" % sid)
        self.assertIn('<option value="Love Letters" selected>Love Letters (old, not on the list)</option>', html)
        data = {"title": "T", "content": "word " * 60, "author": "A", "category": "Love Letters"}
        self.c.post("/admin/stories/edit/%d" % sid, headers=ORIGIN, data=dict(data, csrf_token=self.token()))
        with app.app_context():
            self.assertEqual(db.session.get(Story, sid).category, "Love Letters")
        self.c.post("/admin/stories/edit/%d" % sid, headers=ORIGIN,
                    data=dict(data, category="Family", csrf_token=self.token()))
        with app.app_context():
            self.assertEqual(db.session.get(Story, sid).category, "Family")
        # a made-up value from a tampered form is still not accepted
        self.c.post("/admin/stories/edit/%d" % sid, headers=ORIGIN,
                    data=dict(data, category="Hacked", csrf_token=self.token()))
        with app.app_context():
            self.assertEqual(db.session.get(Story, sid).category, "General")

    def test_review_page_flags_a_category_not_on_the_list(self):
        self.login()
        self.stories_in({"Love Letters": 1, "Campus": 1})
        with app.app_context():
            ids = {s.category: s.id for s in Story.query.all()}
        self.assertIn("Love Letters (not on the category list)", self.html("/admin/stories/review/%d" % ids["Love Letters"]))
        html = self.html("/admin/stories/review/%d" % ids["Campus"])
        self.assertIn("Campus", html)
        self.assertNotIn("not on the category list", html)


# ───────────────────────── chips and More ─────────────────────────

class ChipTests(Step6Base):
    def test_empty_categories_hidden_and_counts_shown(self):
        self.stories_in({"Life": 3, "Campus": 1})
        with app.app_context():
            db.session.add(Story(title="Draft", slug="draft", category="Sports", content="w " * 60,
                                 author="A", published=False))
            db.session.commit()
        html = self.html("/stories")
        shown, more = self.chips(html)
        self.assertEqual(shown, ["Life", "Campus"])        # Sports has only a draft
        self.assertEqual(more, [])
        self.assertNotIn('id="chipsMore"', html)           # nothing more to show
        self.assertRegex(html, r'class="chip" aria-current="page">All</a>')
        self.assertIn('<span>Life</span><span class="chip-count"><span class="visually-hidden">, </span>3'
                      '<span class="visually-hidden"> stories</span></span>', html)
        self.assertIn('1<span class="visually-hidden"> story</span>', html)

    def test_order_by_count_first_eight_other_last_rest_in_more(self):
        self.stories_in({"Other": 9, "General": 1, "Life": 7, "News": 6, "Campus": 5, "Family": 4,
                         "Sports": 3, "Politics": 2, "Technology": 2, "Novels": 1, "18+": 2})
        shown, more = self.chips(self.html("/stories"))
        # ties (2, 1) keep the list order; Other is last even with the most stories
        self.assertEqual(shown, ["Life", "News", "Campus", "Family", "Sports", "Politics", "Technology", "18+"])
        self.assertEqual(more, ["General", "Novels", "Other"])

    def test_other_last_among_the_chips_when_it_fits(self):
        self.stories_in({"Other": 5, "Life": 1, "News": 2})
        self.assertEqual(self.chips(self.html("/stories"))[0], ["News", "Life", "Other"])

    def test_more_panel_markup(self):
        self.stories_in({c: 1 for c in C.CATEGORIES[:10]})
        html = self.html("/stories?sort=read")
        self.assertIn('<details class="chips-more" id="chipsMore">', html)
        self.assertRegex(html, r'<summary class="chip chip-more">\s*<span>More<span class="visually-hidden"> categories</span></span>')
        self.assertIn('<span class="chip-count" aria-hidden="true">+2</span>', html)
        self.assertIn('<div class="chips-more-panel" role="group" aria-label="More categories">', html)
        shown, more = self.chips(html)
        self.assertEqual(len(shown), 8)
        self.assertEqual(more, ["Campus", "Family"])            # equal counts: list order
        # plain links (work without JavaScript) that keep the sort
        self.assertIn('href="/stories?category=family&amp;sort=read"', html)

    def test_selected_category_always_a_chip(self):
        self.stories_in({c: 2 for c in C.CATEGORIES[:9]})       # Campus is 9th: in More
        self.assertIn("Campus", self.chips(self.html("/stories"))[1])
        html = self.html("/stories?category=campus")
        shown, more = self.chips(html)
        self.assertIn("Campus", shown)
        self.assertNotIn("Campus", more)
        self.assertRegex(html, r'class="chip" aria-current="page">\s*<span>Campus</span>')
        # an empty (but real) category: still a chip, count 0, normal empty message
        html = self.html("/stories?category=politics")
        self.assertIn("Politics", self.chips(html)[0])
        self.assertIn("Nothing matches", html)
        self.assertIn("Politics stories", html)

    def test_chip_links_use_the_slug_and_keep_search_and_sort(self):
        self.stories_in({"Mystery & Horror": 1, "18+": 1})
        html = self.html("/stories?search=t&sort=loved")
        self.assertIn('href="/stories?category=mystery-horror&amp;search=t&amp;sort=loved"', html)
        self.assertIn('href="/stories?category=18-plus&amp;search=t&amp;sort=loved"', html)
        html = self.html("/stories?category=mystery-horror")
        self.assertIn('<input type="hidden" name="category" value="mystery-horror">', html)


# ───────────────────────── URL matching ─────────────────────────

class UrlTests(Step6Base):
    def setUp(self):
        super().setUp()
        self.stories_in({"Mystery & Horror": 2, "Life": 1})

    def test_name_or_slug_any_case(self):
        for raw in ("mystery-horror", "Mystery%20%26%20Horror", "MYSTERY-HORROR", "mystery+and+horror"):
            html = self.html("/stories?category=" + raw)
            self.assertIn("Mystery &amp; Horror stories", html, raw)
            self.assertIn("2 stories", html, raw)
        self.assertIn("Life stories", self.html("/stories?category=LIFE"))

    def test_unknown_category_is_a_normal_empty_page(self):
        r = self.c.get("/stories?category=dragons")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("No stories found", html)
        self.assertIn("There is no “dragons” category.", html)
        self.assertIn('See all stories</a>', html)
        self.assertIn('<meta name="robots" content="noindex">', html)
        self.assertIn("0 stories", html)
        self.assertNotIn('class="chip" aria-current="page">All<', html)   # nothing selected
        self.assertNotIn('id="continueReading"', html)
        self.assertNotIn("card-featured", html)

    def test_unknown_category_text_is_escaped(self):
        html = self.html("/stories?category=%3Cscript%3Ealert(1)%3C/script%3E")
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)

    def test_story_page_tag_links_to_the_slug(self):
        with app.app_context():
            slug = Story.query.filter_by(category="Mystery & Horror").first().slug
        self.assertIn('href="/stories?category=mystery-horror" class="tag">Mystery &amp; Horror</a>',
                      self.html("/stories/" + slug))


# ───────────────────────── 18+ unchanged ─────────────────────────

class AdultTests(Step6Base):
    def test_adult_category_behaves_as_before(self):
        self.stories_in({"18+": 1})
        html = self.html("/stories?category=18-plus")
        self.assertIn("Adult story. You will be asked to confirm your age", html)
        self.assertNotIn("word word", html)
        self.assertEqual(self.html("/stories?category=18%2B").count("Adult story."), 1)
        gate = self.html("/stories/t1")                      # the age gate, not the story
        self.assertIn("/stories/t1/verify-age", gate)
        self.assertNotIn("word word word", gate)
        self.c.get("/stories/t1/verify-age")
        self.assertIn("word word word", self.html("/stories/t1"))


# ───────────────────────── API and Mkuu ─────────────────────────

class ApiTests(Step6Base):
    def setUp(self):
        super().setUp()
        os.environ["FEED_TOKEN"] = TOKEN
        self.h = {"X-Feed-Token": TOKEN}
        self.stories_in({"Mystery & Horror": 2, "Life": 1, "18+": 1})

    def get(self, query):
        r = self.c.get("/api/stories" + query, headers=self.h)
        self.assertEqual(r.status_code, 200, query)
        return r.get_json()

    def test_category_filter_by_name_or_slug(self):
        for q in ("?category=mystery-horror", "?category=Mystery%20%26%20Horror", "?category=MYSTERY+and+horror"):
            data = self.get(q)
            self.assertEqual(data["category"], "Mystery & Horror", q)
            self.assertEqual(data["total"], 2, q)
            self.assertEqual({s["category_slug"] for s in data["stories"]}, {"mystery-horror"})
        self.assertEqual(self.get("?category=life")["total"], 1)
        self.assertEqual(self.get("")["total"], 3)                 # no filter; never 18+
        self.assertIsNone(self.get("")["category"])

    def test_unknown_or_adult_category_gives_an_empty_list(self):
        for q in ("?category=dragons", "?category=%3Cx%3E", "?category=18-plus", "?category=18%2B"):
            data = self.get(q)
            self.assertEqual((data["total"], data["stories"]), (0, []), q)

    def test_categories_endpoint(self):
        r = self.c.get("/api/categories", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "public, max-age=300")
        items = r.get_json()["categories"]
        self.assertEqual([x["name"] for x in items], C.CATEGORIES)           # the one list, in order
        by_name = {x["name"]: x for x in items}
        self.assertEqual(by_name["Mystery & Horror"], {"name": "Mystery & Horror", "slug": "mystery-horror",
                                                       "count": 2, "adult": False})
        self.assertEqual(by_name["Life"]["count"], 1)
        self.assertEqual(by_name["Sports"]["count"], 0)
        self.assertTrue(by_name["18+"]["adult"])
        with app.app_context():                                             # drafts are not counted
            db.session.add(Story(title="D", slug="d", category="Sports", content="w " * 60, author="A", published=False))
            db.session.commit()
        self.assertEqual({x["name"]: x["count"] for x in self.c.get("/api/categories", headers=self.h).get_json()["categories"]}["Sports"], 0)

    def test_categories_endpoint_has_the_feed_rules(self):
        for headers in ({}, {"X-Feed-Token": "wrong"}):                     # token set: needed, like the feed
            self.assertEqual(self.c.get("/api/categories", headers=headers).status_code, 401)
        os.environ.pop("FEED_TOKEN")                                        # no token configured: open, per-IP limit
        for _ in range(S.FEED_IP_LIMIT):
            self.assertEqual(self.c.get("/api/categories").status_code, 200)
        r = self.c.get("/api/categories")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)

    def test_sort_labels_short_only_on_small_screens(self):
        css = open(os.path.join(ROOT, "static", "css", "stories.css"), encoding="utf-8").read()
        self.assertIn(".sort-short { display: none; }", css)
        m = re.search(r"\.sort \{ flex-wrap: nowrap; max-width: 100%; \}.*?@media \(max-width: 479\.98px\) \{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(m)
        self.assertIn(".sort-short { display: inline; }", m.group(1))
        self.assertRegex(m.group(1), r"\.sort-full \{[^}]*clip")            # hidden visually, still the name

    def test_mkuu_unknown_category_falls_back_to_general(self):
        for sent, stored in (("Dragons", "General"), ("", "General"), (None, "General"), (["Life"], "General"),
                             (42, "General"), ("mystery and horror", "Mystery & Horror"), ("TECHNOLOGY", "Technology")):
            S.limiter._hits.clear()
            body = dict(SUBMISSION, category=sent, title="From Mkuu %r" % (sent,))
            r = self.c.post("/api/submissions", json=body, headers=self.h)
            self.assertEqual(r.status_code, 201, (sent, r.get_json()))
            with app.app_context():
                self.assertEqual(Story.query.filter_by(slug=r.get_json()["slug"]).one().category, stored, sent)


if __name__ == "__main__":
    unittest.main()
