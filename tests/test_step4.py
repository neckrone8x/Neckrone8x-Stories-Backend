"""Step 4: reaction and comment counts on cards, ?sort=, the Trending row,
and the two in-sync reaction bars on the story page."""
import re
import unittest
from datetime import datetime, timedelta

from sqlalchemy import event

from test_app_step1 import HOST, AppBase, Story, app, db  # sets up a temp DB

import app as app_module
from app import Comment, Reaction, StoryRead

ORIGIN = {"Origin": HOST}
FETCH = {"Origin": HOST, "X-Requested-With": "fetch", "Accept": "application/json"}


class Step4Base(AppBase):
    def sid(self, slug):
        with app.app_context():
            return Story.query.filter_by(slug=slug).one().id

    def react(self, slug, *types, when=None):
        sid = self.sid(slug)
        with app.app_context():
            for i, t in enumerate(types):
                r = Reaction(story_id=sid, reaction_type=t, ip_address="v-%s-%d" % (slug, i))
                if when:
                    r.created_at = when
                db.session.add(r)
            db.session.commit()

    def reads(self, slug, n, when=None):
        sid = self.sid(slug)
        with app.app_context():
            for i in range(n):
                db.session.add(StoryRead(story_id=sid, ip_address="r-%d" % i,
                                         read_at=when or datetime.utcnow()))
            db.session.commit()

    def comments(self, slug, approved=0, held=0):
        sid = self.sid(slug)
        with app.app_context():
            for i in range(approved):
                db.session.add(Comment(story_id=sid, name="A", content="ok %d" % i, approved=True))
            for i in range(held):
                db.session.add(Comment(story_id=sid, name="B", content="https://x %d" % i, approved=False))
            db.session.commit()

    def html(self, url="/stories"):
        return self.c.get(url).get_data(as_text=True)

    def card(self, html, slug):
        """The <article> of one story card."""
        for block in re.findall(r"<article class=\"card[^\"]*\">.*?</article>", html, re.S):
            if 'href="/stories/%s"' % slug in block:
                return block
        self.fail("no card for %s" % slug)

    def order(self, html):
        return re.findall(r'href="/stories/([a-z0-9-]+)" class="card-link"', html)


# ───────────────────────── 1. counts on cards ─────────────────────────

class CardCountTests(Step4Base):
    def test_top_three_reactions_total_and_approved_comments(self):
        now = datetime.utcnow()
        self.add_story(title="Loved", slug="loved", created_at=now)
        self.add_story(title="Quiet", slug="quiet", created_at=now - timedelta(hours=1))
        self.add_story(title="Third", slug="third", created_at=now - timedelta(hours=2))
        self.react("loved", "love", "love", "love", "laugh", "laugh", "sad", "like")
        self.comments("loved", approved=2, held=1)

        html = self.html("/stories?sort=read")              # no featured card: all in the grid
        card = self.card(html, "loved")
        found = re.findall(r'<li class="rx rx-(\w+)".*?<span class="rx-n">(\d+)</span>', card, re.S)
        # top 3 by count; the tie (like/sad = 1) goes to the first in the usual order
        self.assertEqual(found, [("love", "3"), ("laugh", "2"), ("like", "1")])
        self.assertIn("7 reactions", card)
        self.assertIn("2 comments", card)                   # the held one is not counted
        self.assertIn('<span class="visually-hidden">Love:</span>', card)

        quiet = self.card(html, "quiet")
        self.assertIn("No reactions yet", quiet)
        self.assertIn("0 comments", quiet)
        self.assertNotIn('class="rx rx-', quiet)

    def test_featured_card_has_counts_too_and_singulars(self):
        self.add_story(title="One", slug="one")
        self.react("one", "wow")
        self.comments("one", approved=1)
        card = self.card(self.html(), "one")
        self.assertIn("card-featured", card)
        self.assertIn("1 reaction<", card)
        self.assertIn("1 comment<", card)

    def test_counts_use_a_fixed_number_of_queries(self):
        """Grouped queries for the whole page: a page of 6 cards costs the same
        as a page of 1 (both pages need the total count, so they compare fairly)."""
        def queries_for_list(url):
            app_module.clear_trending_cache()
            seen = []

            def count(*a, **k):
                seen.append(1)
            with app.app_context():
                event.listen(db.engine, "before_cursor_execute", count)
                try:
                    self.assertEqual(self.c.get(url).status_code, 200)
                finally:
                    event.remove(db.engine, "before_cursor_execute", count)
            return len(seen)

        now = datetime.utcnow()
        for i in range(7):                                  # page 1: 6 cards, page 2: 1 card
            self.add_story(title="S%d" % i, slug="s%d" % i, created_at=now - timedelta(hours=i))
            self.react("s%d" % i, "love", "like")
            self.comments("s%d" % i, approved=2)
        # a category page has no Trending row, so only the list itself is measured
        six = queries_for_list("/stories?category=Life&sort=read")
        one = queries_for_list("/stories?category=Life&sort=read&page=2")
        self.assertEqual(len(self.order(self.html("/stories?category=Life&sort=read"))), 6)
        self.assertEqual(len(self.order(self.html("/stories?category=Life&sort=read&page=2"))), 1)
        self.assertEqual(one, six)
        self.assertLessEqual(six, 5)   # stories, total count, reactions, comments, chip counts


# ───────────────────────── 2. sorting ─────────────────────────

class SortTests(Step4Base):
    def setUp(self):
        super().setUp()
        now = datetime.utcnow()
        # newest: a, b, c   most read: c, a, b   most loved: b, c, a
        self.add_story(title="A", slug="a", created_at=now, read_count=50)
        self.add_story(title="B", slug="b", created_at=now - timedelta(days=1), read_count=5)
        self.add_story(title="C", slug="c", created_at=now - timedelta(days=2), read_count=90)
        self.react("b", "love", "like", "wow")
        self.react("c", "sad", "love")

    def test_three_orders(self):
        self.assertEqual(self.order(self.html("/stories")), ["a", "b", "c"])
        self.assertEqual(self.order(self.html("/stories?sort=newest")), ["a", "b", "c"])
        self.assertEqual(self.order(self.html("/stories?sort=read")), ["c", "a", "b"])
        self.assertEqual(self.order(self.html("/stories?sort=loved")), ["b", "c", "a"])
        self.assertEqual(self.order(self.html("/stories?sort=nonsense")), ["a", "b", "c"])

    def test_ties_fall_back_to_newest(self):
        self.react("a", "love", "like", "wow")              # a now ties b on 3 reactions
        self.assertEqual(self.order(self.html("/stories?sort=loved")), ["a", "b", "c"])

    def test_control_marks_the_current_sort_and_featured_only_on_newest(self):
        html = self.html("/stories?sort=read")
        # full label = the link's name; the short one (shown below 480px) is hidden from screen readers
        self.assertRegex(html, r'href="/stories\?sort=read"\s+class="sort-option" aria-current="true">'
                               r'<span class="sort-full">Most read</span><span class="sort-short" aria-hidden="true">Read</span><')
        self.assertIn('href="/stories"\n                   class="sort-option"><span class="sort-full">Newest</span>', html)
        self.assertIn('<span class="sort-full">Most loved</span><span class="sort-short" aria-hidden="true">Loved</span>', html)
        self.assertIn("Most read stories", html)
        self.assertNotIn("card-featured", html)
        self.assertIn("card-featured", self.html("/stories"))
        self.assertIn('aria-label="Sort stories"', html)

    def test_sort_works_with_search_category_and_paging(self):
        now = datetime.utcnow()
        for i in range(8):                                  # read counts 10..80
            self.add_story(title="Mango %d" % i, slug="mango-%d" % i, category="Life",
                           created_at=now - timedelta(hours=i), read_count=10 * (i + 1))
        page1 = self.html("/stories?search=mango&sort=read")
        self.assertEqual(self.order(page1), ["mango-%d" % i for i in (7, 6, 5, 4, 3, 2)])
        self.assertIn("8 stories", page1)
        self.assertIn('href="/stories?page=2&amp;search=mango&amp;sort=read"', page1)
        self.assertIn('<input type="hidden" name="sort" value="read">', page1)
        self.assertIn('href="/stories?category=life&amp;search=mango&amp;sort=read"', page1)
        page2 = self.html("/stories?search=mango&sort=read&page=2")
        self.assertEqual(self.order(page2), ["mango-1", "mango-0"])
        self.assertIn('href="/stories?page=1&amp;search=mango&amp;sort=read"', page2)
        cat = self.html("/stories?category=life&sort=read")
        self.assertEqual(self.order(cat)[:3], ["c", "mango-7", "mango-6"])   # C (90 reads) is Life too
        self.assertIn('href="/stories?category=life&amp;sort=loved"', cat)  # sort links keep the category (slug)


# ───────────────────────── 3. trending ─────────────────────────

class TrendingTests(Step4Base):
    def setUp(self):
        super().setUp()
        now = datetime.utcnow()
        for slug in ("t1", "t2", "t3", "t4"):
            self.add_story(title=slug.upper(), slug=slug, created_at=now)
        self.add_story(title="Adult", slug="adult", category="18+", created_at=now)

    def trending(self, html):
        m = re.search(r'<section class="trending".*?</section>', html, re.S)
        return re.findall(r'href="/stories/([a-z0-9-]+)" class="card-link trend-link"', m.group(0)) if m else None

    def test_hidden_when_fewer_than_two_qualify(self):
        self.assertIsNone(self.trending(self.html()))       # no activity at all
        self.react("t1", "love")
        app_module.clear_trending_cache()
        self.assertIsNone(self.trending(self.html()))       # only one story
        old = datetime.utcnow() - timedelta(days=8)
        self.react("t2", "love", "love", when=old)          # too old to count
        self.reads("t3", 3, when=old)
        app_module.clear_trending_cache()
        self.assertIsNone(self.trending(self.html()))
        self.react("adult", "love", "love", "love")         # 18+ is never in the row
        app_module.clear_trending_cache()
        self.assertIsNone(self.trending(self.html()))

    def test_shown_ranked_and_capped_at_three(self):
        self.react("t1", "love")                            # score 2
        self.reads("t2", 5)                                 # score 5
        self.react("t3", "love", "wow")                     # score 4 + 1 read = 5, more reactions
        self.reads("t3", 1)
        self.reads("t4", 1)                                 # score 1
        html = self.html()
        self.assertEqual(self.trending(html), ["t3", "t2", "t1"])
        self.assertIn("Trending this week", html)
        self.assertIn("2 reactions\n                                · 1 read this week", html)

    def test_only_on_page_one_without_search_or_category(self):
        self.react("t1", "love")
        self.react("t2", "like")
        self.assertIsNotNone(self.trending(self.html()))
        self.assertIsNotNone(self.trending(self.html("/stories?sort=loved")))
        for url in ("/stories?search=t", "/stories?category=Life", "/stories?page=2"):
            self.assertIsNone(self.trending(self.html(url)), url)

    def test_cached_for_five_minutes(self):
        self.react("t1", "love")
        self.react("t2", "like")
        self.assertEqual(self.trending(self.html()), ["t2", "t1"])   # equal scores: newer first
        self.reads("t4", 20)                                # would now be first...
        self.assertEqual(self.trending(self.html()), ["t2", "t1"])   # ...but the cached ranking stays
        app_module._trending_cache["at"] -= app_module.TRENDING_TTL + 1   # 5 minutes later
        self.assertEqual(self.trending(self.html())[0], "t4")

    def test_unpublished_story_drops_out_at_once(self):
        self.react("t1", "love")
        self.react("t2", "like")
        self.react("t3", "wow")
        self.assertEqual(len(self.trending(self.html())), 3)
        with app.app_context():
            Story.query.filter_by(slug="t1").one().published = False
            db.session.commit()
        self.assertEqual(self.trending(self.html()), ["t3", "t2"])   # equal scores: newer first


# ───────────────────────── 4. reactions in both places ─────────────────────────

class StoryReactionTests(Step4Base):
    def setUp(self):
        super().setUp()
        self.add_story()

    def bars(self, html):
        top = re.search(r'<div class="reactions-top".*?</div>\s*</div>', html, re.S).group(0)
        bottom = re.search(r'<section class="panel" id="reactions".*?</section>', html, re.S).group(0)
        return top, bottom

    def test_two_bars_with_labels_counts_and_the_title_order(self):
        self.react("hello", "love", "love", "wow")
        html = self.html("/stories/hello")
        top, bottom = self.bars(html)
        self.assertLess(html.index('class="story-byline"'), html.index('class="reactions-top"'))
        self.assertLess(html.index('class="reactions-top"'), html.index('class="story-body"'))
        self.assertIn('aria-label="React to this story"', top)
        for bar in (top, bottom):
            self.assertEqual(len(re.findall(r'data-reaction="(\w+)"', bar)), 5)
            for label in ("Love", "Like", "Laugh", "Wow", "Sad"):
                self.assertIn('<span class="reaction-label">%s</span>' % label, bar)
            self.assertIn('data-reaction="love"', bar)
            self.assertRegex(bar, r'(?s)data-reaction="love".*?<span class="reaction-count">2</span>')
            self.assertRegex(bar, r'(?s)data-reaction="like".*?<span class="reaction-count is-zero">0</span>')
        self.assertIn('id="reactionStatus" role="status" aria-live="polite"', html)

    def test_own_reaction_highlighted_in_both(self):
        self.c.post("/stories/hello/react/laugh", headers=ORIGIN)     # plain form post still works
        html = self.html("/stories/hello")
        top, bottom = self.bars(html)
        for bar in (top, bottom):
            self.assertRegex(bar, r'class="reaction is-active"\s+data-reaction="laugh"[^>]*aria-pressed="true"')
            self.assertEqual(bar.count('aria-pressed="true"'), 1)

    def test_background_reaction_returns_the_state_for_both_bars(self):
        r = self.c.post("/stories/hello/react/love", headers=FETCH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        data = r.get_json()
        self.assertEqual(data["mine"], "love")
        self.assertEqual(data["counts"], {"love": 1, "like": 0, "laugh": 0, "wow": 0, "sad": 0})
        self.assertEqual(data["total"], 1)
        data = self.c.post("/stories/hello/react/wow", headers=FETCH).get_json()     # swap
        self.assertEqual((data["mine"], data["counts"]["love"], data["counts"]["wow"]), ("wow", 0, 1))
        data = self.c.post("/stories/hello/react/wow", headers=FETCH).get_json()     # same again: removed
        self.assertIsNone(data["mine"])
        self.assertEqual(data["total"], 0)

    def test_background_errors_are_json(self):
        r = self.c.post("/stories/hello/react/angry", headers=FETCH)
        self.assertEqual((r.status_code, r.get_json()["error"]), (400, "unknown reaction"))
        for _ in range(60):
            self.c.post("/stories/hello/react/love", headers=FETCH)
        r = self.c.post("/stories/hello/react/love", headers=FETCH)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(r.get_json()["error"], "too many reactions")
        # from another site it is still refused (Origin check)
        r = self.c.post("/stories/hello/react/love", headers={"Origin": "https://evil.example", "X-Requested-With": "fetch"})
        self.assertEqual(r.status_code, 403)

    def test_buttons_have_aria_labels_with_counts(self):
        self.react("hello", "love", "love")
        top, bottom = self.bars(self.html("/stories/hello"))
        for bar in (top, bottom):
            self.assertIn('aria-label="Love, 2 reactions"', bar)
            self.assertIn('aria-label="Like, 0 reactions"', bar)
        self.react("hello", "sad")
        self.assertIn('aria-label="Sad, 1 reaction"', self.html("/stories/hello"))

    def test_top_row_is_compact_on_small_phones(self):
        import os
        css = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "static", "css", "stories.css"), encoding="utf-8").read()
        m = re.search(r"@media \(max-width: 479\.98px\) \{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(m)
        rules = m.group(1)
        self.assertIn(".reactions-top .reactions", rules)
        self.assertIn("flex-wrap: nowrap", rules)
        self.assertIn("min-width: var(--tap)", rules)                  # 44px tap size kept
        self.assertRegex(rules, r"\.reactions-top \.reaction-label \{[^}]*clip")   # hidden visually, still read
        self.assertNotIn(".reactions-bottom", rules)                   # bottom set keeps its labels

    def test_script_keeps_both_bars_in_step(self):
        import os
        js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "static", "js", "stories.js"), encoding="utf-8").read()
        self.assertIn("querySelectorAll('.reaction[data-reaction]')", js)   # every button, both bars
        self.assertIn("'X-Requested-With': 'fetch'", js)
        self.assertIn("aria-pressed", js)
        self.assertIn("is-zero", js)


if __name__ == "__main__":
    unittest.main()
