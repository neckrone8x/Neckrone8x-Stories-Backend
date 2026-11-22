"""Step 6: one category list (categories.py) used everywhere, in popularity
order; the first 8 chips always in the main row (18+ eighth) and the rest in
the More panel, tolerant ?category= matching, and unknown
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
ORDER = ["Relationship", "Confession", "Campus", "Novels", "Marriage", "Family", "Experience", "18+",
         "Mystery & Horror", "Life", "News", "Politics", "Sports", "Technology", "General", "Other"]
FIRST8, REST = ORDER[:8], ORDER[8:]
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
        self.assertEqual(C.CATEGORIES, ORDER)                       # popularity order
        self.assertEqual(C.CATEGORIES[7], "18+")
        self.assertEqual(C.CATEGORIES[-1], "Other")
        self.assertEqual(C.CHIPS_SHOWN, 8)
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
        html = self.html("/stories/submit")
        self.assertEqual(options(html), ORDER)
        self.assertIn('<option value="General" selected>', html)          # still the default

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
        html = self.html("/admin/create-story")
        self.assertEqual(options(html), ORDER)
        self.assertIn('<option value="General" selected>', html)
        self.stories_in({"Life": 1})
        with app.app_context():
            sid = Story.query.one().id
        self.assertEqual(options(self.html("/admin/stories/edit/%d" % sid)), ORDER)

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
    def test_first_eight_always_in_the_row_rest_in_more(self):
        """The live case: 7 categories with stories, several of the first 8 empty."""
        self.stories_in({"Relationship": 4, "Confession": 3, "General": 1, "Experience": 1,
                         "News": 1, "Life": 1, "Novels": 1})
        with app.app_context():
            db.session.add(Story(title="Draft", slug="draft", category="Campus", content="w " * 60,
                                 author="A", published=False))
            db.session.commit()
        html = self.html("/stories")
        shown, more = self.chips(html)
        self.assertEqual(shown, FIRST8)                                        # list order, not counts
        self.assertEqual(more, REST)                                           # the rest, Other last
        self.assertEqual(more[-1], "Other")
        self.assertIn('<details class="chips-more" id="chipsMore">', html)
        # empty (and draft-only) categories show no count at all, not "0"
        for slug, name in (("campus", "Campus"), ("marriage", "Marriage"), ("18-plus", "18+"),
                           ("sports", "Sports")):
            self.assertRegex(html, r'href="/stories\?category=%s" class="chip">\s*<span>%s</span>\s*</a>'
                             % (re.escape(slug), re.escape(name)))
        self.assertNotIn('</span>0<span class="visually-hidden">', html)
        self.assertRegex(html, r'class="chip" aria-current="page">All</a>')
        self.assertIn('<span>Relationship</span><span class="chip-count"><span class="visually-hidden">, </span>4'
                      '<span class="visually-hidden"> stories</span></span>', html)
        self.assertIn('<span>Life</span><span class="chip-count"><span class="visually-hidden">, </span>1'
                      '<span class="visually-hidden"> story</span></span>', html)       # counts in More too

    def test_first_eight_shown_even_with_no_stories(self):
        html = self.html("/stories")
        shown, more = self.chips(html)
        self.assertEqual(shown, FIRST8)
        self.assertEqual(more, REST)
        self.assertNotIn('class="chip-count"', html)

    def test_counts_do_not_reorder_the_row(self):
        self.stories_in({"Other": 9, "General": 1, "Life": 7, "News": 6, "Campus": 5, "Family": 4,
                         "Sports": 3, "Politics": 2, "Technology": 2, "Novels": 1, "18+": 2})
        shown, more = self.chips(self.html("/stories"))
        self.assertEqual(shown, FIRST8)
        self.assertEqual(more, REST)

    def test_more_panel_markup(self):
        self.stories_in({c: 1 for c in C.CATEGORIES})
        html = self.html("/stories?sort=read")
        self.assertIn('<details class="chips-more" id="chipsMore">', html)
        self.assertRegex(html, r'<summary class="chip chip-more">\s*<span>More<span class="visually-hidden"> categories</span></span>\s*</summary>')
        self.assertIn('<div class="chips-more-panel" role="group" aria-label="More categories">', html)
        shown, more = self.chips(html)
        self.assertEqual(len(shown), 8)
        self.assertEqual(len(more), 8)
        self.assertEqual(html.count('class="chip-count"'), 16)
        # plain links (work without JavaScript) that keep the sort
        self.assertIn('href="/stories?category=mystery-horror&amp;sort=read"', html)
        self.assertIn('href="/stories?category=18-plus&amp;sort=read"', html)

    def test_selected_more_category_joins_the_row_highlighted(self):
        self.stories_in({"Life": 2})
        self.assertNotIn("Life", self.chips(self.html("/stories"))[0])
        html = self.html("/stories?category=life")
        shown, more = self.chips(html)
        self.assertEqual(shown, FIRST8 + ["Life"])
        self.assertEqual(more, REST)
        self.assertRegex(html, r'class="chip" aria-current="page">\s*<span>Life</span>')
        self.assertNotRegex(html, r'aria-current="page">\s*<span>(?!Life)')
        # one of the first 8 selected: the row stays the same 8
        shown, _ = self.chips(self.html("/stories?category=campus"))
        self.assertEqual(shown, FIRST8)

    def test_chip_row_fits_a_phone_in_both_themes(self):
        css = open(os.path.join(ROOT, "static", "css", "stories.css"), encoding="utf-8").read()
        self.assertIn(".chips { position: relative; display: flex; flex-wrap: wrap; gap: 8px; }", css)
        rule = re.search(r"\.chip \{(.*?)\}", css, re.S).group(1)
        for prop in ("background: var(--surface)", "color: var(--text)", "border: 1px solid var(--border)"):
            self.assertIn(prop, rule)                                      # theme tokens, no fixed colours

    def test_empty_category_page_invites_a_story(self):
        self.stories_in({"Life": 1})
        for slug, name in (("politics", "Politics"), ("mystery-horror", "Mystery &amp; Horror"),
                           ("marriage", "Marriage")):
            r = self.c.get("/stories?category=" + slug)
            self.assertEqual(r.status_code, 200)
            html = r.get_data(as_text=True)
            self.assertIn(name.replace("&amp;", "&"), [x for x in self.chips(html)[0]])
            self.assertIn(name + " stories", html)
            self.assertIn("No stories here yet. Be the first to share one.", html)
            self.assertIn('href="/stories/submit?category=%s" class="button">' % slug, html)
            self.assertNotIn("Nothing matches", html)
        # a search inside a category that has stories keeps the search wording
        self.assertIn("Nothing matches", self.html("/stories?category=life&search=zzz"))

    def test_share_form_opens_with_the_category_chosen(self):
        html = self.html("/stories/submit?category=mystery-horror")
        self.assertIn('<option value="Mystery &amp; Horror" selected>', html)
        self.assertEqual(options(html), ORDER)
        html = self.html("/stories/submit?category=dragons")                # unknown: the usual default
        self.assertIn('<option value="General" selected>', html)

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

    def test_adult_chip_is_eighth_and_its_list_shows_no_story_text(self):
        html = self.html("/stories")
        self.assertEqual(self.chips(html)[0][7], "18+")                 # in the row even with no stories
        self.stories_in({"18+": 2})
        html = self.html("/stories?category=18-plus")
        self.assertEqual(html.count("Adult story. You will be asked to confirm your age"), 2)
        self.assertNotIn("word word", html)
        for slug in ("t1", "t2"):                                        # each story keeps its own gate
            self.assertIn("/stories/%s/verify-age" % slug, self.html("/stories/" + slug))


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
        self.assertEqual([x["name"] for x in items], ORDER)                  # the one list, in order
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

    def test_new_categories_end_to_end_for_mkuu(self):
        """Each new category: shared from Mkuu, published, then found by its slug in the feed."""
        for name in NEW:
            S.limiter._hits.clear()
            r = self.c.post("/api/submissions", headers=self.h,
                            json=dict(SUBMISSION, category=name, title="Mkuu story about " + name))
            self.assertEqual(r.status_code, 201, name)
            with app.app_context():
                story = Story.query.filter_by(slug=r.get_json()["slug"]).one()
                self.assertEqual(story.category, name)
                story.published = True
                db.session.commit()
            slug = C.category_slug(name)
            data = self.get("?category=" + slug)
            self.assertEqual(data["category"], name, name)
            self.assertIn("Mkuu story about " + name, [s["title"] for s in data["stories"]])
            self.assertTrue(all(s["category"] == name and s["category_slug"] == slug for s in data["stories"]))
        counts = {x["name"]: x["count"] for x in self.c.get("/api/categories", headers=self.h).get_json()["categories"]}
        self.assertEqual(counts["Campus"], 1)
        self.assertEqual(counts["Mystery & Horror"], 3)                 # 2 from setUp + 1 from Mkuu

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
