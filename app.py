from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
    redirect,
    url_for,
    session
)

from flask_sqlalchemy import SQLAlchemy

from datetime import datetime, timedelta

import logging
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()

from site_security import (
    COMMENT_MAX, COMMENT_NAME_MAX, AUTHOR_MAX, CATEGORIES, CONTENT_MAX,
    TITLE_MAX, admin_required, canonical_category, check_admin_credentials,
    clean_category, clean_text, configure_security, csrf_required, feed_authorized,
    feed_authorized_strict, feed_rate_ok, limit_ok,
    like_escape, login_blocked, login_failed, make_excerpt, make_slug,
    rate_ok, reading_minutes, safe_https_url, start_admin_session,
    token_post, visitor_key,
)
from categories import (
    ADULT_CATEGORY, CATEGORY_LIST, CHIPS_SHOWN, category_slug, find_category,
)


# ============================================================
# APP CONFIGURATION
# ============================================================

app = Flask(__name__)

# Secret key, admin login check, safe cookies, same-site form checks and
# security headers all live in site_security.py. On Render the site refuses to
# start unless SECRET_KEY, ADMIN_USERNAME and an admin password are set.
configure_security(app)


# ============================================================
# TEMPLATE GLOBALS
# ============================================================

MAIN_SITE_URL = "https://neckrone8x.co.ke/"


def absolute_image_url(value):
    """A full https address for og:image / twitter:image. Cover images are
    saved as full https links; an older site-relative path ("/static/x.jpg")
    is made absolute. Anything else gives None (no image tag)."""
    full = safe_https_url(value)
    if full:
        return full
    v = (value or "").strip()
    if v.startswith("/") and not v.startswith("//"):
        return safe_https_url(request.url_root.rstrip("/") + v)
    return None


_COMMENT_URL = re.compile(r"https?://[^\s<>\"']+", re.I)


@app.template_filter("comment_text")
def comment_text(comment):
    """A comment as safe HTML. All text is escaped first. Only a comment the
    admin approved with "trust link" gets clickable http(s) links (marked
    nofollow / ugc); any other comment shows its addresses as plain text."""
    from markupsafe import Markup, escape

    safe = str(escape(comment.content or ""))
    if not getattr(comment, "link_trusted", False):
        return Markup(safe)

    def to_link(match):
        url = match.group(0)
        tail = ""
        while url and url[-1] in ".,;:!?)":
            tail = url[-1] + tail
            url = url[:-1]
        return ('<a href="%s" rel="nofollow ugc noopener noreferrer" '
                'target="_blank">%s</a>%s' % (url, url, tail))

    return Markup(_COMMENT_URL.sub(to_link, safe))


@app.context_processor
def inject_template_globals():
    return {
        "current_year": datetime.utcnow().year,
        "absolute_image_url": absolute_image_url,
        "main_site_url": MAIN_SITE_URL,
        "excerpt": make_excerpt,
        "reading_minutes": reading_minutes,
        "reaction_types": REACTION_TYPES,
        "reaction_labels": REACTION_LABELS,
        # the one category list (categories.py), for every form and link
        "categories": CATEGORIES,
        "category_slug": category_slug,
        "canonical_category": canonical_category,
    }


# ============================================================
# GET VISITOR IP ADDRESS
# ============================================================

def get_visitor_ip():
    """Who is reading. Despite the old name this is NOT the IP address: it is a
    private key made from a random cookie (many people on a mobile network
    share one IP), and only a keyed hash of it is stored."""
    return visitor_key()


# ============================================================
# DATABASE
# ============================================================

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

database_url = os.getenv("DATABASE_URL")

if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        "sqlite:///" + os.path.join(BASE_DIR, "stories.db")
    )

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# After Render free sleeps, Postgres may have closed the old connections. Test
# each one before use (and renew it after ~5 minutes) instead of failing the
# first request with a 500.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}

db = SQLAlchemy(app)


# ============================================================
# STORY MODEL
# ============================================================

class Story(db.Model):

    __tablename__ = "stories"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(220), unique=True, nullable=False)
    category = db.Column(db.String(100), nullable=False, default="General")
    content = db.Column(db.Text, nullable=False)
    author = db.Column(db.String(100), nullable=False, default="Anonymous")
    cover_image = db.Column(db.String(500), nullable=True)
    # Set when the story was shared from the Mkuu app by a verified member
    verified_tier = db.Column(db.String(16), nullable=True)
    show_mark = db.Column(db.Boolean, default=True)
    published = db.Column(db.Boolean, default=False)
    read_count = db.Column(db.Integer, default=0)
    # created_at = when it was written / submitted (never changes).
    # published_at = when it was approved or published (set by mark_published).
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    published_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reads = db.relationship("StoryRead", backref="story", cascade="all, delete-orphan")
    reactions = db.relationship("Reaction", backref="story", cascade="all, delete-orphan")
    comments = db.relationship("Comment", backref="story", cascade="all, delete-orphan")

    @property
    def shown_at(self):
        """The date readers see: the publish date, or for an old row that has
        none yet, the submission date."""
        return self.published_at or self.created_at

    def mark_published(self):
        """Publish now. The first time, the publish date is set to this moment;
        a story that was published before keeps its original publish date. The
        submission date (created_at) never changes."""
        self.published = True
        if self.published_at is None:
            self.published_at = datetime.utcnow()


def publish_order():
    """Newest published first (old rows without a publish date fall back to
    their submission date)."""
    return (db.func.coalesce(Story.published_at, Story.created_at).desc(),
            Story.id.desc())


# ============================================================
# STORY READ MODEL
# ============================================================

class StoryRead(db.Model):

    __tablename__ = "story_reads"

    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(
        db.Integer,
        db.ForeignKey("stories.id", ondelete="CASCADE"),
        nullable=False
    )
    ip_address = db.Column(db.String(45), nullable=True)
    read_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ============================================================
# REACTION MODEL
# ============================================================

class Reaction(db.Model):

    __tablename__ = "reactions"

    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(
        db.Integer,
        db.ForeignKey("stories.id", ondelete="CASCADE"),
        nullable=False
    )
    reaction_type = db.Column(db.String(20), nullable=False)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ============================================================
# COMMENT MODEL
# ============================================================

class Comment(db.Model):

    __tablename__ = "comments"

    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(
        db.Integer,
        db.ForeignKey("stories.id", ondelete="CASCADE"),
        nullable=False
    )
    name = db.Column(db.String(80), nullable=False, default="Anonymous")
    content = db.Column(db.Text, nullable=False)
    ip_address = db.Column(db.String(45), nullable=True)
    # False = held for review (a comment with a link). Only approved ones are public.
    approved = db.Column(db.Boolean, default=True, nullable=False)
    # Set by "Approve and trust link": its web addresses are shown as links.
    link_trusted = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    return redirect(url_for("stories"))


# ============================================================
# PUBLIC STORIES
# ============================================================

@app.route("/stories")
def stories():

    search = request.args.get("search", "").strip()[:100]
    # ?category= accepts the name or the slug in any case ("life", "Life",
    # "mystery-horror", "Mystery & Horror"). An unknown one is not an error:
    # the page says nothing was found.
    category_arg = request.args.get("category", "").strip()[:100]
    found = find_category(category_arg) if category_arg else None
    category = found.name if found else ""
    unknown_category = category_arg if category_arg and not found else ""
    page = request.args.get("page", 1, type=int)

    if page < 1:
        page = 1

    query = Story.query.filter_by(published=True)

    if search:
        search_pattern = f"%{like_escape(search)}%"
        query = query.filter(
            db.or_(
                Story.title.ilike(search_pattern, escape="\\"),
                Story.author.ilike(search_pattern, escape="\\"),
                Story.category.ilike(search_pattern, escape="\\"),
                Story.content.ilike(search_pattern, escape="\\")
            )
        )

    if category:
        query = query.filter(db.func.lower(Story.category) == category.lower())
    elif unknown_category:
        query = query.filter(db.false())

    sort = request.args.get("sort", "newest")
    if sort not in SORTS:
        sort = "newest"

    pagination = sorted_stories(query, sort).paginate(
        page=page, per_page=6, error_out=False
    )

    stories = pagination.items

    chips, more_chips = category_chips(category)

    # The Trending row: page 1 of the plain list only.
    trending = []
    if pagination.page == 1 and not search and not category and not unknown_category:
        trending = trending_stories()

    return render_template(
        "stories/index.html",
        stories=stories,
        search=search,
        category=category,
        unknown_category=unknown_category,
        chips=chips,
        more_chips=more_chips,
        pagination=pagination,
        sort=sort,
        sorts=SORTS,
        stats=card_stats(stories + [story for story, _ in trending]),
        trending=trending,
    )


# ============================================================
# LIST HELPERS: category chips, sorting, counts for a page of cards, trending
# ============================================================

def category_story_counts():
    """Published stories per category (one grouped query), by display name.
    Stored names are matched to the list without caring about case."""
    rows = db.session.query(db.func.lower(Story.category), db.func.count(Story.id)).filter(
        Story.published == True).group_by(db.func.lower(Story.category)).all()
    by_lower = dict(rows)
    return {c.name: by_lower.get(c.name.lower(), 0) for c in CATEGORY_LIST}


def category_chips(selected=""):
    """The chips under the search box, and the list for the More panel.

    Main row: always the first 8 categories of categories.py (18+ is the
    eighth), even ones with no stories yet, plus the selected category if it
    is one from More. More: the rest, in the same order (Other last).
    Each item: {"name", "slug", "count"}; the template hides a count of 0."""
    counts = category_story_counts()
    every = [{"name": c.name, "slug": c.slug, "count": counts[c.name]} for c in CATEGORY_LIST]
    shown, more = every[:CHIPS_SHOWN], every[CHIPS_SHOWN:]
    if selected and selected not in [x["name"] for x in shown]:
        shown = shown + [next((x for x in more if x["name"] == selected),
                              {"name": selected, "slug": category_slug(selected), "count": counts.get(selected, 0)})]
    return shown, more


SORTS = {"newest": "Newest", "read": "Most read", "loved": "Most loved"}


def sorted_stories(query, sort):
    """Newest (publish date), Most read (reads) or Most loved (all reactions).
    Ties fall back to the newest first."""
    if sort == "read":
        return query.order_by(db.func.coalesce(Story.read_count, 0).desc(), *publish_order())
    if sort == "loved":
        totals = db.session.query(
            Reaction.story_id.label("sid"), db.func.count(Reaction.id).label("n")
        ).group_by(Reaction.story_id).subquery()
        return query.outerjoin(totals, totals.c.sid == Story.id).order_by(
            db.func.coalesce(totals.c.n, 0).desc(), *publish_order())
    return query.order_by(*publish_order())


def card_stats(stories):
    """Reaction and approved-comment counts for every card on the page, with
    two grouped queries in total (not one per card)."""
    ids = sorted({s.id for s in stories})
    stats = {i: {"counts": {}, "total": 0, "top": [], "comments": 0} for i in ids}
    if not ids:
        return stats

    for sid, rtype, n in db.session.query(
        Reaction.story_id, Reaction.reaction_type, db.func.count(Reaction.id)
    ).filter(Reaction.story_id.in_(ids)).group_by(
        Reaction.story_id, Reaction.reaction_type
    ):
        if rtype in REACTION_TYPES:
            stats[sid]["counts"][rtype] = n
            stats[sid]["total"] += n

    for sid, n in db.session.query(
        Comment.story_id, db.func.count(Comment.id)
    ).filter(Comment.story_id.in_(ids), Comment.approved == True).group_by(
        Comment.story_id
    ):
        stats[sid]["comments"] = n

    for item in stats.values():
        item["top"] = sorted(
            item["counts"].items(),
            key=lambda kv: (-kv[1], REACTION_TYPES.index(kv[0]))
        )[:3]
    return stats


TRENDING_DAYS = 7
TRENDING_SIZE = 3
TRENDING_MIN = 2
TRENDING_TTL = 300  # seconds
_trending_cache = {"at": None, "items": []}


def compute_trending():
    """Top stories of the last 7 days: score = 2 x reactions + reads (a
    reaction is a stronger signal than an open). 18+ stories are left out of
    this public row. Returns [{"id", "reactions", "reads"}], best first."""
    since = datetime.utcnow() - timedelta(days=TRENDING_DAYS)
    reactions = dict(db.session.query(
        Reaction.story_id, db.func.count(Reaction.id)
    ).filter(Reaction.created_at >= since).group_by(Reaction.story_id).all())
    reads = dict(db.session.query(
        StoryRead.story_id, db.func.count(StoryRead.id)
    ).filter(StoryRead.read_at >= since).group_by(StoryRead.story_id).all())

    candidates = set(reactions) | set(reads)
    if not candidates:
        return []
    allowed = {sid for (sid,) in db.session.query(Story.id).filter(
        Story.id.in_(candidates), Story.published == True, Story.category != ADULT_CATEGORY)}

    items = [{"id": sid, "reactions": reactions.get(sid, 0), "reads": reads.get(sid, 0)}
             for sid in allowed]
    items.sort(key=lambda x: (-(2 * x["reactions"] + x["reads"]), -x["reactions"], -x["id"]))
    return items[:TRENDING_SIZE]


def clear_trending_cache():
    _trending_cache["at"] = None
    _trending_cache["items"] = []


def trending_stories():
    """[(story, {"reactions", "reads"}), ...] or [] when fewer than 2 qualify.
    The ranking is kept in memory for 5 minutes; the stories themselves are
    loaded fresh, so an unpublished one drops out at once."""
    now = time.monotonic()
    if _trending_cache["at"] is None or now - _trending_cache["at"] > TRENDING_TTL:
        _trending_cache["items"] = compute_trending()
        _trending_cache["at"] = now
    items = _trending_cache["items"]
    if len(items) < TRENDING_MIN:
        return []

    found = {s.id: s for s in Story.query.filter(
        Story.id.in_([x["id"] for x in items]),
        Story.published == True, Story.category != ADULT_CATEGORY)}
    rows = [(found[x["id"]], x) for x in items if x["id"] in found]
    return rows if len(rows) >= TRENDING_MIN else []


# ============================================================
# VIEW SINGLE STORY
# ============================================================

@app.route("/stories/<slug>")
def view_story(slug):

    story = Story.query.filter_by(slug=slug, published=True).first_or_404()

    # 18+ AGE GATE
    if story.category == ADULT_CATEGORY:
        verified_stories = session.get("age_verified_stories", [])
        if not isinstance(verified_stories, list):
            verified_stories = []
        if story.id not in verified_stories:
            return render_template("stories/age_gate.html", story=story)

    # RECORD ONE READ PER VISITOR PER STORY
    visitor_ip = get_visitor_ip()

    existing_read = StoryRead.query.filter_by(
        story_id=story.id,
        ip_address=visitor_ip
    ).first()

    if not existing_read and rate_ok("read", 40, 3600):
        story.read_count = (story.read_count or 0) + 1
        db.session.add(StoryRead(story_id=story.id, ip_address=visitor_ip))
        db.session.commit()

    # REACTIONS
    reaction_counts = reaction_counts_for(story.id)
    user_reaction_type = reaction_of(story.id, visitor_ip)

    # COMMENTS
    comments = Comment.query.filter_by(
        story_id=story.id,
        approved=True
    ).order_by(Comment.created_at.desc()).all()

    response = make_response(render_template(
        "stories/story.html",
        story=story,
        reaction_counts=reaction_counts,
        user_reaction_type=user_reaction_type,
        comments=comments,
        related=related_stories(story),
        comment_held=(request.args.get("comment") == "held")
    ))
    if story.category == ADULT_CATEGORY:
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


RELATED_SIZE = 3


def related_stories(story):
    """Up to 3 other published stories from the same category, most loved
    first (ties: newest). Only called after the age gate, so an 18+ story's
    related list (all 18+, since it is the same category) is only shown to
    a reader who has already confirmed their age; every one of those links
    still asks again on its own page. Any other story never gets 18+ ones."""
    query = Story.query.filter(
        Story.published == True,
        Story.id != story.id,
        db.func.lower(Story.category) == (story.category or "").lower(),
    )
    if story.category != ADULT_CATEGORY:
        query = query.filter(Story.category != ADULT_CATEGORY)
    return sorted_stories(query, "loved").limit(RELATED_SIZE).all()


# ============================================================
# SAVED STORIES AND CONTINUE READING
# The reader's list lives in their browser (no accounts). The page sends the
# slugs it holds and gets back only the published stories among them.
# ============================================================

SAVED_MAX = 50
_SLUG_SHAPE = re.compile(r"^[a-z0-9][a-z0-9-]{0,219}$")


def requested_slugs(raw):
    """Valid, distinct slugs in the order given, at most SAVED_MAX."""
    slugs = []
    for part in (raw or "")[:SAVED_MAX * 230].split(","):
        part = part.strip().lower()
        if _SLUG_SHAPE.match(part) and part not in slugs:
            slugs.append(part)
            if len(slugs) == SAVED_MAX:
                break
    return slugs


def reader_item(story):
    adult = story.category == ADULT_CATEGORY
    return {
        "slug": story.slug,
        "title": story.title,
        "author": story.author,
        "category": story.category,
        "adult": adult,
        "excerpt": None if adult else make_excerpt(story.content, 150),  # no 18+ text
        "reading_minutes": reading_minutes(story.content),
        "read_count": story.read_count or 0,
        "published_at": story.shown_at.strftime("%Y-%m-%d"),
        "published_label": story.shown_at.strftime("%b %d, %Y"),
        "url": url_for("view_story", slug=story.slug),
    }


@app.route("/stories/saved")
def saved_stories():
    response = make_response(render_template("stories/saved.html"))
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.route("/stories/saved/data")
def saved_stories_data():
    if not rate_ok("saved", 60, 60):
        return jsonify(error="too many requests"), 429
    slugs = requested_slugs(request.args.get("slugs"))
    found = {}
    if slugs:
        found = {s.slug: s for s in Story.query.filter(
            Story.slug.in_(slugs), Story.published == True)}
    response = jsonify(stories=[reader_item(found[s]) for s in slugs if s in found])
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


# ============================================================
# 18+ AGE VERIFICATION
# ============================================================

@app.route("/stories/<slug>/verify-age")
def verify_age(slug):

    story = Story.query.filter_by(slug=slug, published=True).first_or_404()

    if story.category != ADULT_CATEGORY:
        return redirect(url_for("view_story", slug=story.slug))

    verified_stories = session.get("age_verified_stories", [])
    if not isinstance(verified_stories, list):
        verified_stories = []

    if story.id not in verified_stories:
        verified_stories.append(story.id)

    session["age_verified_stories"] = verified_stories
    session.modified = True

    return redirect(url_for("view_story", slug=story.slug))


# ============================================================
# REACT TO STORY
# ============================================================

REACTION_TYPES = ("love", "like", "laugh", "wow", "sad")
REACTION_LABELS = {"love": "Love", "like": "Like", "laugh": "Laugh", "wow": "Wow", "sad": "Sad"}


def reaction_counts_for(story_id):
    counts = {rtype: 0 for rtype in REACTION_TYPES}
    for rtype, total in db.session.query(
        Reaction.reaction_type, db.func.count(Reaction.id)
    ).filter(Reaction.story_id == story_id).group_by(Reaction.reaction_type).all():
        if rtype in counts:
            counts[rtype] = total
    return counts


def reaction_of(story_id, visitor):
    mine = Reaction.query.filter_by(story_id=story_id, ip_address=visitor).first()
    return mine.reaction_type if mine else None


def wants_json():
    """The story page's script sends this header; a plain form post does not."""
    return request.headers.get("X-Requested-With") == "fetch"


def reaction_state(story_id, visitor):
    counts = reaction_counts_for(story_id)
    return {"counts": counts, "total": sum(counts.values()),
            "mine": reaction_of(story_id, visitor)}


@app.route("/stories/<slug>/react/<reaction_type>", methods=["POST"])
def react_to_story(slug, reaction_type):

    story = Story.query.filter_by(slug=slug, published=True).first_or_404()

    if reaction_type not in REACTION_TYPES:
        if wants_json():
            return jsonify(error="unknown reaction"), 400
        return redirect(url_for("view_story", slug=slug))

    if not rate_ok("react", 60, 3600):
        if wants_json():
            return jsonify(error="too many reactions"), 429
        return redirect(url_for("view_story", slug=slug) + "#reactions")

    visitor_ip = get_visitor_ip()

    existing = Reaction.query.filter_by(
        story_id=story.id,
        ip_address=visitor_ip
    ).first()

    if existing:
        if existing.reaction_type == reaction_type:
            # Same reaction — remove it
            db.session.delete(existing)
        else:
            # Different reaction — swap it
            existing.reaction_type = reaction_type
    else:
        db.session.add(Reaction(
            story_id=story.id,
            reaction_type=reaction_type,
            ip_address=visitor_ip
        ))

    db.session.commit()

    if wants_json():
        response = jsonify(reaction_state(story.id, visitor_ip))
        response.headers["Cache-Control"] = "no-store"
        return response
    return redirect(url_for("view_story", slug=slug) + "#reactions")


# ============================================================
# ADD COMMENT
# ============================================================

@app.route("/stories/<slug>/comment", methods=["POST"])
def add_comment(slug):

    story = Story.query.filter_by(slug=slug, published=True).first_or_404()

    name = clean_text(request.form.get("name"), COMMENT_NAME_MAX)
    content = clean_text(request.form.get("content"), COMMENT_MAX)

    if not content or not rate_ok("comment", 5, 600):
        return redirect(url_for("view_story", slug=slug) + "#comments")

    if not name:
        name = "Anonymous"

    # A comment with a link is held back (spam); all others show at once.
    has_link = bool(re.search(r"https?://|www\.", content, re.I))

    db.session.add(Comment(
        story_id=story.id,
        name=name,
        content=content,
        ip_address=get_visitor_ip(),
        approved=not has_link
    ))
    db.session.commit()

    if has_link:
        return redirect(url_for("view_story", slug=slug, comment="held") + "#comments")
    return redirect(url_for("view_story", slug=slug) + "#comments")


# ============================================================
# PUBLIC STORY SUBMISSION
# ============================================================

@app.route("/stories/submit", methods=["GET", "POST"])
def submit_story():

    if request.method == "POST":

        if not rate_ok("submit", 5, 3600):
            return render_template(
                "stories/submit.html",
                error="You have sent several stories already. "
                      "Please try again in a while."
            ), 429

        title = clean_text(request.form.get("title"), TITLE_MAX)
        category = clean_category(request.form.get("category"))
        content = clean_text(request.form.get("content"), CONTENT_MAX)
        author = clean_text(request.form.get("author"), AUTHOR_MAX)

        if not title or not content:
            return render_template(
                "stories/submit.html",
                error="Title and story content are required."
            )

        slug = make_slug(
            title, lambda x: Story.query.filter_by(slug=x).first() is not None
        )

        story = Story(
            title=title,
            slug=slug,
            category=category,
            content=content,
            author=(author or "Anonymous"),
            published=False,
            read_count=0
        )

        db.session.add(story)
        db.session.commit()

        return render_template("stories/submit_thanks.html")

    return render_template("stories/submit.html", error=None)


# ============================================================
# ADMIN LOGIN
# ============================================================

@app.route("/admin/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        if login_blocked():
            return render_template(
                "admin/login.html",
                error="Too many wrong attempts. Please try again in 15 minutes."
            ), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if check_admin_credentials(username, password):
            start_admin_session()
            return redirect(url_for("dashboard"))

        login_failed()

        return render_template(
            "admin/login.html",
            error="Invalid username or password."
        )

    return render_template("admin/login.html")


# ============================================================
# ADMIN LOGOUT
# ============================================================

@app.route("/admin/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("login"))


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
@admin_required
def dashboard():

    published_count = Story.query.filter_by(published=True).count()
    pending_count = Story.query.filter_by(published=False).count()

    total_reads = db.session.query(
        db.func.sum(Story.read_count)
    ).filter(Story.published == True).scalar() or 0

    now = datetime.utcnow()

    shown_at = db.func.coalesce(Story.published_at, Story.created_at)
    stories_this_month = Story.query.filter(
        Story.published == True,
        db.extract("month", shown_at) == now.month,
        db.extract("year", shown_at) == now.year
    ).count()

    held_comments = Comment.query.filter_by(approved=False).count()

    recent_stories = Story.query.order_by(
        Story.created_at.desc()
    ).limit(5).all()

    top_stories = Story.query.filter_by(published=True).order_by(
        Story.read_count.desc()
    ).limit(5).all()

    pending_stories = pending_in_review_order()

    return render_template(
        "admin/dashboard.html",
        published_count=published_count,
        pending_count=pending_count,
        total_reads=total_reads,
        stories_this_month=stories_this_month,
        recent_stories=recent_stories,
        pending_stories=pending_stories,
        top_stories=top_stories,
        pending_submissions=pending_count,
        held_comments=held_comments
    )


# ============================================================
# CREATE STORY
# ============================================================

@app.route("/admin/create-story", methods=["GET", "POST"])
@admin_required
@csrf_required
def create_story():

    if request.method == "POST":

        title = clean_text(request.form.get("title"), TITLE_MAX)
        category = clean_category(request.form.get("category"))
        content = clean_text(request.form.get("content"), CONTENT_MAX)
        author = clean_text(request.form.get("author"), AUTHOR_MAX)
        cover_image = safe_https_url(request.form.get("cover_image"))
        published = (request.form.get("published") == "on")

        if not title or not content:
            return render_template(
                "admin/create_story.html",
                error="Title and story content are required."
            )

        slug = make_slug(
            title, lambda x: Story.query.filter_by(slug=x).first() is not None
        )

        story = Story(
            title=title,
            slug=slug,
            category=category,
            content=content,
            author=(author or "Anonymous"),
            cover_image=cover_image,
            published=False,
            read_count=0
        )
        if published:
            story.mark_published()

        db.session.add(story)
        db.session.commit()

        return redirect(url_for("manage_stories"))

    return render_template("admin/create_story.html")


# ============================================================
# MANAGE PUBLISHED STORIES
# ============================================================

@app.route("/admin/stories")
@admin_required
def manage_stories():

    stories = Story.query.filter_by(published=True).order_by(
        *publish_order()
    ).all()

    return render_template("admin/stories.html", stories=stories)


# ============================================================
# MANAGE SUBMISSIONS
# ============================================================

def pending_in_review_order():
    """Pending stories: verified members first (founder, developer, paid), then
    everyone else. Newest first inside each group. Sorted here, not in SQL, so it
    works with any version of the database library (the queue is small)."""
    rank = {"founder": 0, "developer": 1, "paid": 2}
    stories = Story.query.filter_by(published=False).all()
    stories.sort(key=lambda s: s.created_at or datetime.min, reverse=True)
    stories.sort(key=lambda s: rank.get(s.verified_tier or "", 3))
    return stories


@app.route("/admin/submissions")
@admin_required
def manage_submissions():

    submissions = pending_in_review_order()

    return render_template("admin/submissions.html", submissions=submissions)


# ============================================================
# REVIEW STORY
# ============================================================

@app.route("/admin/stories/review/<int:story_id>")
@admin_required
def review_story(story_id):

    story = Story.query.get_or_404(story_id)

    return render_template("admin/review_story.html", story=story)


# ============================================================
# APPROVE STORY
# ============================================================

@app.route("/admin/stories/approve/<int:story_id>", methods=["POST"])
@admin_required
@csrf_required
def approve_story(story_id):

    story = Story.query.get_or_404(story_id)
    story.mark_published()
    db.session.commit()

    return redirect(url_for("dashboard"))


# ============================================================
# EDIT STORY
# ============================================================

@app.route("/admin/stories/edit/<int:story_id>", methods=["GET", "POST"])
@admin_required
@csrf_required
def edit_story(story_id):

    story = Story.query.get_or_404(story_id)

    if request.method == "POST":

        title = clean_text(request.form.get("title"), TITLE_MAX)
        posted = request.form.get("category")
        if posted is not None and posted == story.category and not canonical_category(posted):
            category = story.category      # an old category not on the list: left as it is
        else:
            category = clean_category(posted)
        content = clean_text(request.form.get("content"), CONTENT_MAX)
        author = clean_text(request.form.get("author"), AUTHOR_MAX)

        if not title or not content:
            return render_template(
                "admin/edit_story.html",
                story=story,
                error="Title and story content are required."
            )

        story.title = title
        story.category = category
        story.content = content
        story.author = (author or "Anonymous")
        story.cover_image = safe_https_url(request.form.get("cover_image"))

        db.session.commit()

        return redirect(url_for("manage_stories"))

    return render_template("admin/edit_story.html", story=story)


# ============================================================
# STORY ANALYTICS
# ============================================================

@app.route("/admin/stories/analytics/<int:story_id>")
@admin_required
def story_analytics(story_id):

    story = Story.query.get_or_404(story_id)

    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = today_start - timedelta(days=now.weekday())
    month_start = datetime(now.year, now.month, 1)

    total_reads = StoryRead.query.filter_by(story_id=story.id).count()

    today_reads = StoryRead.query.filter(
        StoryRead.story_id == story.id,
        StoryRead.read_at >= today_start
    ).count()

    week_reads = StoryRead.query.filter(
        StoryRead.story_id == story.id,
        StoryRead.read_at >= week_start
    ).count()

    month_reads = StoryRead.query.filter(
        StoryRead.story_id == story.id,
        StoryRead.read_at >= month_start
    ).count()

    if story.read_count != total_reads:
        story.read_count = total_reads
        db.session.commit()

    unique_readers = db.session.query(
        db.func.count(db.func.distinct(StoryRead.ip_address))
    ).filter(
        StoryRead.story_id == story.id,
        StoryRead.ip_address.isnot(None)
    ).scalar() or 0

    reads_per_reader = (
        round(total_reads / unique_readers, 1) if unique_readers else 0
    )

    seven_days_ago = today_start - timedelta(days=6)

    day_rows = db.session.query(
        db.func.date(StoryRead.read_at).label("read_day"),
        db.func.count(StoryRead.id).label("read_total")
    ).filter(
        StoryRead.story_id == story.id,
        StoryRead.read_at.isnot(None),
        StoryRead.read_at >= seven_days_ago,
        StoryRead.read_at < (today_start + timedelta(days=1))
    ).group_by(
        db.func.date(StoryRead.read_at)
    ).order_by(
        db.func.date(StoryRead.read_at).asc()
    ).all()

    read_lookup = {str(row.read_day): row.read_total for row in day_rows}

    daily_reads = []
    for i in range(7):
        current_day = seven_days_ago + timedelta(days=i)
        current_day_string = current_day.strftime("%Y-%m-%d")
        daily_reads.append({
            "date": current_day_string,
            "reads": read_lookup.get(current_day_string, 0)
        })

    if day_rows:
        best_row = max(day_rows, key=lambda row: row.read_total)
        best_day_date = str(best_row.read_day)
        best_day_reads = best_row.read_total
        try:
            best_day_label = datetime.strptime(
                best_day_date, "%Y-%m-%d"
            ).strftime("%B %d, %Y")
        except ValueError:
            best_day_label = best_day_date
        best_day = {"date": best_day_label, "reads": best_day_reads}
    else:
        best_day = {"date": "No reads yet", "reads": 0}
        best_day_reads = 0
        best_day_label = "No reads yet"

    if daily_reads:
        max_daily_reads = max(item["reads"] for item in daily_reads)
        total_daily_reads = sum(item["reads"] for item in daily_reads)
        average_daily_reads = round(total_daily_reads / len(daily_reads), 1)
    else:
        max_daily_reads = 0
        average_daily_reads = 0

    recent_reads = StoryRead.query.filter_by(
        story_id=story.id
    ).order_by(
        StoryRead.read_at.desc()
    ).limit(10).all()

    for read in recent_reads:
        if not read.read_at:
            read.relative_time = "Unknown"
            continue
        seconds = (now - read.read_at).total_seconds()
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            read.relative_time = "Just now"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            read.relative_time = (
                f"{minutes} minute" + ("s" if minutes != 1 else "") + " ago"
            )
        elif seconds < 86400:
            hours = int(seconds // 3600)
            read.relative_time = (
                f"{hours} hour" + ("s" if hours != 1 else "") + " ago"
            )
        elif seconds < 172800:
            read.relative_time = "Yesterday"
        else:
            read.relative_time = read.read_at.strftime("%B %d, %Y")

    return render_template(
        "admin/analytics.html",
        story=story,
        total_reads=total_reads,
        today_reads=today_reads,
        week_reads=week_reads,
        month_reads=month_reads,
        unique_readers=unique_readers,
        reads_per_reader=reads_per_reader,
        best_day=best_day,
        best_day_reads=best_day_reads,
        best_day_label=best_day_label,
        daily_reads=daily_reads,
        max_daily_reads=max_daily_reads,
        average_daily_reads=average_daily_reads,
        recent_reads=recent_reads
    )


# ============================================================
# OVERALL ANALYTICS
# ============================================================

@app.route("/admin/analytics")
@admin_required
def admin_analytics():

    total_reads = db.session.query(
        db.func.sum(Story.read_count)
    ).filter(Story.published == True).scalar() or 0

    published_stories = Story.query.filter_by(published=True).count()

    unique_readers = db.session.query(
        db.func.count(db.func.distinct(StoryRead.ip_address))
    ).join(
        Story, StoryRead.story_id == Story.id
    ).filter(
        Story.published == True,
        StoryRead.ip_address.isnot(None)
    ).scalar() or 0

    reads_per_reader = (
        round(total_reads / unique_readers, 1) if unique_readers else 0
    )

    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = today_start - timedelta(days=now.weekday())
    month_start = datetime(now.year, now.month, 1)

    today_reads = db.session.query(StoryRead.id).join(
        Story, StoryRead.story_id == Story.id
    ).filter(
        Story.published == True,
        StoryRead.read_at >= today_start
    ).count()

    week_reads = db.session.query(StoryRead.id).join(
        Story, StoryRead.story_id == Story.id
    ).filter(
        Story.published == True,
        StoryRead.read_at >= week_start
    ).count()

    month_reads = db.session.query(StoryRead.id).join(
        Story, StoryRead.story_id == Story.id
    ).filter(
        Story.published == True,
        StoryRead.read_at >= month_start
    ).count()

    top_stories = Story.query.filter_by(published=True).order_by(
        Story.read_count.desc()
    ).limit(5).all()

    daily_rows = db.session.query(
        db.func.date(StoryRead.read_at).label("read_day"),
        db.func.count(StoryRead.id).label("read_total")
    ).join(
        Story, StoryRead.story_id == Story.id
    ).filter(
        Story.published == True,
        StoryRead.read_at.isnot(None)
    ).group_by(
        db.func.date(StoryRead.read_at)
    ).order_by(
        db.func.date(StoryRead.read_at).asc()
    ).all()

    daily_reads = [
        {"date": str(row.read_day), "reads": row.read_total}
        for row in daily_rows
    ]

    return render_template(
        "admin/admin_analytics.html",
        total_reads=total_reads,
        total_stories=published_stories,
        unique_readers=unique_readers,
        reads_per_reader=reads_per_reader,
        today_reads=today_reads,
        week_reads=week_reads,
        month_reads=month_reads,
        top_stories=top_stories,
        daily_reads=daily_reads
    )


# ============================================================
# STORY READS
# ============================================================

@app.route("/admin/stories/reads/<int:story_id>")
@admin_required
def story_reads(story_id):

    story = Story.query.get_or_404(story_id)

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1

    pagination = StoryRead.query.filter_by(story_id=story.id).order_by(
        StoryRead.read_at.desc()
    ).paginate(page=page, per_page=20, error_out=False)

    reads = pagination.items

    total_reads = StoryRead.query.filter_by(story_id=story.id).count()

    unique_readers = db.session.query(
        db.func.count(db.func.distinct(StoryRead.ip_address))
    ).filter(
        StoryRead.story_id == story.id,
        StoryRead.ip_address.isnot(None)
    ).scalar() or 0

    now = datetime.utcnow()
    today = now.date()

    for read in reads:
        if not read.read_at:
            read.read_at = now
        read_date = read.read_at.date()
        if read_date == today:
            read.date_group = "Today"
        elif read_date == (today - timedelta(days=1)):
            read.date_group = "Yesterday"
        else:
            read.date_group = read.read_at.strftime("%B %d, %Y")

        seconds = (now - read.read_at).total_seconds()
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            read.relative_time = "Just now"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            read.relative_time = (
                f"{minutes} minute" + ("s" if minutes != 1 else "") + " ago"
            )
        elif seconds < 86400:
            hours = int(seconds // 3600)
            read.relative_time = (
                f"{hours} hour" + ("s" if hours != 1 else "") + " ago"
            )
        elif seconds < 172800:
            read.relative_time = "Yesterday"
        else:
            read.relative_time = read.read_at.strftime("%B %d, %Y")

    return render_template(
        "admin/story_reads.html",
        story=story,
        reads=reads,
        pagination=pagination,
        total_reads=total_reads,
        unique_readers=unique_readers
    )


# ============================================================
# DELETE STORY
# ============================================================

@app.route("/admin/stories/delete/<int:story_id>", methods=["POST"])
@admin_required
@csrf_required
def delete_story(story_id):

    story = Story.query.get_or_404(story_id)
    db.session.delete(story)
    db.session.commit()

    return redirect(url_for("manage_stories"))


# ============================================================
# PUBLISH / UNPUBLISH STORY
# ============================================================

@app.route("/admin/stories/toggle/<int:story_id>", methods=["POST"])
@admin_required
@csrf_required
def toggle_story(story_id):

    story = Story.query.get_or_404(story_id)
    if story.published:
        story.published = False  # the publish date is kept until it is published again
    else:
        story.mark_published()
    db.session.commit()

    return redirect(url_for("manage_stories"))


# ============================================================
# ADMIN — COMMENT MODERATION
# ============================================================

COMMENT_TABS = ("held", "approved", "all")


def back_to_comments():
    """Back to the tab and page the admin was on (only ever this admin page)."""
    tab = request.form.get("tab", "held")
    page = request.form.get("page", 1, type=int)
    return redirect(url_for(
        "manage_comments",
        tab=tab if tab in COMMENT_TABS else "held",
        page=page if page and page > 1 else None,
    ))


@app.route("/admin/comments")
@admin_required
def manage_comments():

    tab = request.args.get("tab", "held")
    if tab not in COMMENT_TABS:
        tab = "held"
    page = max(request.args.get("page", 1, type=int), 1)

    query = Comment.query.join(Story, Comment.story_id == Story.id)
    if tab == "held":
        query = query.filter(Comment.approved == False)
    elif tab == "approved":
        query = query.filter(Comment.approved == True)

    pagination = query.order_by(
        Comment.created_at.desc(), Comment.id.desc()
    ).paginate(page=page, per_page=25, error_out=False)

    counts = {
        "held": Comment.query.filter_by(approved=False).count(),
        "approved": Comment.query.filter_by(approved=True).count(),
    }
    counts["all"] = counts["held"] + counts["approved"]

    return render_template(
        "admin/comments.html",
        tab=tab,
        comments=pagination.items,
        pagination=pagination,
        counts=counts,
    )


@app.route("/admin/comments/<int:comment_id>/approve", methods=["POST"])
@admin_required
@csrf_required
def approve_comment(comment_id):

    comment = Comment.query.get_or_404(comment_id)
    comment.approved = True
    comment.link_trusted = False
    db.session.commit()
    return back_to_comments()


@app.route("/admin/comments/<int:comment_id>/approve-trust", methods=["POST"])
@admin_required
@csrf_required
def approve_comment_trusted(comment_id):

    comment = Comment.query.get_or_404(comment_id)
    comment.approved = True
    comment.link_trusted = True
    db.session.commit()
    return back_to_comments()


@app.route("/admin/comments/delete/<int:comment_id>", methods=["POST"])
@admin_required
@csrf_required
def delete_comment(comment_id):

    comment = Comment.query.get_or_404(comment_id)
    db.session.delete(comment)
    db.session.commit()
    return back_to_comments()


# ============================================================
# STORY FEED  (read by the Mkuu AI server; never includes 18+ stories or
# the full text, only a short excerpt and a link to the story)
# ============================================================

def feed_item(story, excerpt_chars):
    cover = safe_https_url(story.cover_image)
    return {
        "id": story.id,
        "slug": story.slug,
        "title": story.title,
        "author": story.author,
        "category": story.category,
        "category_slug": category_slug(story.category),
        "excerpt": make_excerpt(story.content, excerpt_chars),
        "reading_minutes": reading_minutes(story.content),
        "url": url_for("view_story", slug=story.slug, _external=True),
        "cover_image": cover,
        "published_at": story.shown_at.isoformat() + "Z",
        "submitted_at": story.created_at.isoformat() + "Z",
        "updated_at": (story.updated_at or story.created_at).isoformat() + "Z",
    }


def feed_query():
    return Story.query.filter(Story.published == True, Story.category != ADULT_CATEGORY)


@app.route("/api/categories")
def api_categories():
    """The one category list, for Mkuu: name, slug and published-story count.
    Same access and rate rules as the other feed reads. 18+ is listed (marked
    adult) so the list is complete, but the feed itself never returns it."""

    if not feed_authorized(request.headers):
        return jsonify(error="unauthorized"), 401

    if not feed_rate_ok(request.headers):
        return jsonify(error="too many requests"), 429

    counts = category_story_counts()
    response = jsonify(categories=[
        {"name": c.name, "slug": c.slug, "count": counts[c.name], "adult": c.name == ADULT_CATEGORY}
        for c in CATEGORY_LIST
    ])
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.route("/api/stories")
def api_stories():

    if not feed_authorized(request.headers):
        return jsonify(error="unauthorized"), 401

    if not feed_rate_ok(request.headers):
        return jsonify(error="too many requests"), 429

    limit = min(max(request.args.get("limit", 50, type=int), 1), 100)
    page = max(request.args.get("page", 1, type=int), 1)
    excerpt_chars = min(max(request.args.get("excerpt", 240, type=int), 100), 600)

    # ?category= (name or slug, any case). Unknown: an empty list, not an error.
    query = feed_query()
    category_arg = (request.args.get("category") or "").strip()
    found = find_category(category_arg) if category_arg else None
    if found:
        query = query.filter(db.func.lower(Story.category) == found.name.lower())
    elif category_arg:
        query = query.filter(db.false())

    pagination = query.order_by(
        *publish_order()
    ).paginate(page=page, per_page=limit, error_out=False)

    response = jsonify({
        "category": found.name if found else None,
        "stories": [feed_item(x, excerpt_chars) for x in pagination.items],
        "page": page,
        "pages": pagination.pages,
        "total": pagination.total,
    })
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.route("/api/stories/<slug>")
def api_story(slug):

    if not feed_authorized(request.headers):
        return jsonify(error="unauthorized"), 401

    if not feed_rate_ok(request.headers):
        return jsonify(error="too many requests"), 429

    story = feed_query().filter(Story.slug == slug).first()
    if story is None:
        return jsonify(error="not found"), 404

    excerpt_chars = min(max(request.args.get("excerpt", 240, type=int), 100), 600)
    item = feed_item(story, excerpt_chars)

    # The Mkuu helper bubble needs the story's text. Only Mkuu's server, which
    # has the token, may ask for it (?full=1).
    if request.args.get("full") == "1":
        if not feed_authorized_strict(request.headers):
            return jsonify(error="unauthorized"), 401
        item["content"] = story.content[:12000]
        response = jsonify(item)
        response.headers["Cache-Control"] = "no-store"
        return response

    return jsonify(item)


@app.route("/api/stories/<slug>/text")
def api_story_text(slug):
    """The whole text of one story, for Mkuu's in-app reader. Only Mkuu's server
    (which has FEED_TOKEN) may ask. Never 18+, never unpublished."""
    if not feed_authorized_strict(request.headers):
        return jsonify(error="unauthorized"), 401

    if not feed_rate_ok(request.headers):
        return jsonify(error="too many requests"), 429

    story = feed_query().filter(Story.slug == slug).first()
    if story is None:
        return jsonify(error="not found"), 404

    item = feed_item(story, 300)
    item["content"] = story.content[:200000]
    response = jsonify(item)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/submissions", methods=["POST"])
@token_post
def api_submissions():
    """A story shared from the Mkuu app. Only Mkuu's server (which has FEED_TOKEN)
    may use this. It is ALWAYS saved as pending, never published. A verified
    member's story carries their tier, so it is reviewed first.
    @token_post: no same-site Origin check (a server sends none); the token is
    required instead, here and before the request reaches this function."""
    import re as _re

    if not feed_authorized_strict(request.headers):
        return jsonify(error="unauthorized"), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Please check your story and try again."), 400

    title = clean_text(str(data.get("title") or ""), TITLE_MAX)
    content = clean_text(str(data.get("content") or ""), CONTENT_MAX)
    category = clean_category(data.get("category"))   # not text or not on the list: General
    author = clean_text(str(data.get("author") or ""), AUTHOR_MAX) or "Anonymous"
    if len(title) < 3 or len(content) < 100:
        return jsonify(error="Please write a title and a longer story."), 400

    tier = str(data.get("tier") or "").strip().lower()
    if tier not in ("paid", "developer", "founder"):
        tier = ""

    # one author cannot flood the review queue (the key is a one-way stand-in sent by Mkuu)
    author_key = _re.sub(r"[^a-f0-9]", "", str(data.get("author_key") or ""))[:32]
    if not author_key:
        return jsonify(error="Please check your story and try again."), 400
    if not limit_ok("apishare:" + author_key, 8, 3600):
        return jsonify(error="You have shared a lot of stories."), 429

    slug = make_slug(
        title, lambda x: Story.query.filter_by(slug=x).first() is not None
    )
    story = Story(
        title=title,
        slug=slug,
        category=category,
        content=content,
        author=author,
        published=False,
        read_count=0,
        verified_tier=tier or None,
        show_mark=bool(data.get("show_mark", True)) if tier else False,
    )
    db.session.add(story)
    db.session.commit()
    return jsonify(ok=True, status="pending", slug=slug), 201


@app.route("/health")
def health():
    return {"ok": True}


@app.route("/robots.txt")
def robots():
    body = (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Allow: /\n"
        "Sitemap: " + url_for("sitemap", _external=True) + "\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    from xml.sax.saxutils import escape

    rows = feed_query().order_by(*publish_order()).limit(5000).all()
    items = ["<url><loc>%s</loc></url>" % escape(
        url_for("stories", _external=True))]
    for story in rows:
        stamp = max(story.updated_at or story.shown_at, story.shown_at).strftime("%Y-%m-%d")
        items.append("<url><loc>%s</loc><lastmod>%s</lastmod></url>" % (
            escape(url_for("view_story", slug=story.slug, _external=True)),
            stamp))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(items) + "</urlset>"
    )
    return Response(xml, mimetype="application/xml")


# ============================================================
# FRIENDLY ERROR PAGES
# ============================================================

def error_page(code, title, message):
    if request.path.startswith("/api/"):
        return jsonify(error=title), code
    return render_template(
        "stories/error.html", code=code, title=title, message=message
    ), code


@app.errorhandler(403)
def forbidden(error):
    return error_page(
        403, "Not allowed",
        "This request could not be verified. Go back, refresh the page and try again."
    )


@app.errorhandler(404)
def not_found(error):
    return error_page(
        404, "Story not found",
        "That page does not exist, or the story is no longer published."
    )


@app.errorhandler(413)
def too_large(error):
    return error_page(413, "Too long", "That text is too long to send.")


@app.errorhandler(500)
def server_error(error):
    return error_page(
        500, "Something went wrong", "Please try again in a moment."
    )


# ============================================================
# CREATE DATABASE TABLES + SAFE SCHEMA UPGRADE
# ============================================================

schema_log = logging.getLogger("stories.schema")
if not schema_log.handlers:
    _handler = logging.StreamHandler()  # stderr: shows in the Render log
    _handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    schema_log.addHandler(_handler)
schema_log.setLevel(logging.INFO)
schema_log.propagate = False

# Columns added after the first version. db.create_all() makes them on a new
# database; upgrade_schema() adds them to an older one. The SQL is the same on
# SQLite and Postgres except the boolean default (1/0 vs TRUE/FALSE).
NEW_COLUMNS = (
    # table,      column,         SQL type and default (sqlite, postgres)
    ("stories",  "verified_tier", ("VARCHAR(16)", "VARCHAR(16)")),
    ("stories",  "show_mark",     ("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE")),
    ("stories",  "published_at",  ("TIMESTAMP", "TIMESTAMP")),
    ("comments", "link_trusted",  ("BOOLEAN NOT NULL DEFAULT 0", "BOOLEAN NOT NULL DEFAULT FALSE")),
)

# CREATE INDEX IF NOT EXISTS works on SQLite and on Postgres 9.5+.
INDEXES = (
    ("ix_stories_pub_created", "stories (published, created_at)"),
    ("ix_stories_pub_published", "stories (published, published_at)"),
    ("ix_story_reads_story", "story_reads (story_id, ip_address)"),
    ("ix_reactions_story", "reactions (story_id, ip_address)"),
    ("ix_comments_story", "comments (story_id, approved)"),
    ("ix_comments_held", "comments (approved, created_at)"),
)


def _schema_step(label, sql, params=None):
    """Runs one statement in its own transaction. A failure is rolled back and
    logged as a warning; it never stops the site from starting."""
    try:
        result = db.session.execute(db.text(sql), params or {})
        db.session.commit()
        return result, None
    except Exception as error:  # noqa: BLE001 - any failure must not stop start-up
        db.session.rollback()
        message = "%s failed: %s: %s" % (label, type(error).__name__,
                                         str(error).splitlines()[0][:200])
        schema_log.warning("[schema] %s", message)
        return None, message


def upgrade_schema():
    """Brings an existing database up to date. Safe to run any number of times
    and on a database that already has everything: it only adds what the
    database is missing. Returns a short report (also written to the log)."""
    report = {"added": [], "indexes": 0, "backfilled": 0, "warnings": []}
    postgres = db.engine.dialect.name == "postgresql"

    try:
        inspector = db.inspect(db.engine)
        existing = {t: {c["name"] for c in inspector.get_columns(t)}
                    for t in {table for table, _, _ in NEW_COLUMNS}
                    if inspector.has_table(t)}
    except Exception as error:  # noqa: BLE001
        report["warnings"].append("could not read the tables: %s" % type(error).__name__)
        schema_log.warning("[schema] could not read the tables (%s); skipping the upgrade",
                           type(error).__name__)
        return report

    for table, column, (sqlite_type, postgres_type) in NEW_COLUMNS:
        if table not in existing or column in existing[table]:
            continue
        col_type = postgres_type if postgres else sqlite_type
        _, warning = _schema_step(
            "add column %s.%s" % (table, column),
            "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, col_type))
        if warning:
            report["warnings"].append(warning)
        else:
            report["added"].append("%s.%s" % (table, column))
            schema_log.info("[schema] added column %s.%s", table, column)

    for name, target in INDEXES:
        _, warning = _schema_step("index %s" % name,
                                  "CREATE INDEX IF NOT EXISTS %s ON %s" % (name, target))
        if warning:
            report["warnings"].append(warning)
        else:
            report["indexes"] += 1

    # Old published stories have no publish date yet: use their submission
    # date (what readers saw before). Only touches rows where it is empty.
    result, warning = _schema_step(
        "fill publish dates",
        "UPDATE stories SET published_at = created_at "
        "WHERE published_at IS NULL AND published = :yes",
        {"yes": True})
    if warning:
        report["warnings"].append(warning)
    else:
        report["backfilled"] = max(result.rowcount or 0, 0)

    schema_log.info(
        "[schema] upgrade done (%s): added=%s, indexes ok=%d/%d, publish dates filled=%d, warnings=%d",
        "postgres" if postgres else db.engine.dialect.name,
        ",".join(report["added"]) or "none", report["indexes"], len(INDEXES),
        report["backfilled"], len(report["warnings"]))
    return report


with app.app_context():
    db.create_all()
    try:
        upgrade_schema()
    except Exception as error:  # noqa: BLE001 - never block start-up
        db.session.rollback()
        schema_log.warning("[schema] upgrade stopped early: %s", type(error).__name__)


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":

    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"

    app.run(host="0.0.0.0", port=port, debug=debug_mode)