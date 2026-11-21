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

import os
import re

from dotenv import load_dotenv

load_dotenv()

from site_security import (
    COMMENT_MAX, COMMENT_NAME_MAX, AUTHOR_MAX, CATEGORIES, CONTENT_MAX,
    TITLE_MAX, admin_required, canonical_category, check_admin_credentials,
    clean_category, clean_text, configure_security, feed_authorized,
    feed_authorized_strict, feed_rate_ok, limit_ok,
    like_escape, login_blocked, login_failed, make_excerpt, make_slug,
    rate_ok, reading_minutes, safe_https_url, start_admin_session,
    token_post, visitor_key,
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


@app.context_processor
def inject_template_globals():
    return {
        "current_year": datetime.utcnow().year,
        "absolute_image_url": absolute_image_url,
        "main_site_url": MAIN_SITE_URL,
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reads = db.relationship("StoryRead", backref="story", cascade="all, delete-orphan")
    reactions = db.relationship("Reaction", backref="story", cascade="all, delete-orphan")
    comments = db.relationship("Comment", backref="story", cascade="all, delete-orphan")


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
    approved = db.Column(db.Boolean, default=True, nullable=False)
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
    category = request.args.get("category", "").strip()
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

    if canonical_category(category):
        query = query.filter(Story.category == canonical_category(category))

    pagination = query.order_by(Story.created_at.desc()).paginate(
        page=page, per_page=6, error_out=False
    )

    stories = pagination.items

    categories = [
        "General", "Confession", "Experience", "News",
        "Relationship", "Marriage", "Life", "Novels", "18+", "Other"
    ]

    return render_template(
        "stories/index.html",
        stories=stories,
        search=search,
        category=category,
        categories=categories,
        pagination=pagination
    )


# ============================================================
# VIEW SINGLE STORY
# ============================================================

@app.route("/stories/<slug>")
def view_story(slug):

    story = Story.query.filter_by(slug=slug, published=True).first_or_404()

    # 18+ AGE GATE
    if story.category == "18+":
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
    reaction_types = ["love", "like", "laugh", "wow", "sad"]
    reaction_counts = {rtype: 0 for rtype in reaction_types}

    for rtype, total in db.session.query(
        Reaction.reaction_type, db.func.count(Reaction.id)
    ).filter(Reaction.story_id == story.id).group_by(Reaction.reaction_type).all():
        if rtype in reaction_counts:
            reaction_counts[rtype] = total

    user_reaction = Reaction.query.filter_by(
        story_id=story.id,
        ip_address=visitor_ip
    ).first()

    user_reaction_type = user_reaction.reaction_type if user_reaction else None

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
        comments=comments
    ))
    if story.category == "18+":
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# ============================================================
# 18+ AGE VERIFICATION
# ============================================================

@app.route("/stories/<slug>/verify-age")
def verify_age(slug):

    story = Story.query.filter_by(slug=slug, published=True).first_or_404()

    if story.category != "18+":
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

@app.route("/stories/<slug>/react/<reaction_type>", methods=["POST"])
def react_to_story(slug, reaction_type):

    story = Story.query.filter_by(slug=slug, published=True).first_or_404()

    if reaction_type not in {"love", "like", "laugh", "wow", "sad"}:
        return redirect(url_for("view_story", slug=slug))

    if not rate_ok("react", 60, 3600):
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

        return """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Story Submitted — Neckrone8x</title>
            <style>
                * { box-sizing: border-box; }
                body {
                    margin: 0; min-height: 100vh;
                    display: flex; justify-content: center; align-items: center;
                    background: #080c14; color: white;
                    font-family: Arial, sans-serif;
                    text-align: center; padding: 20px;
                }
                .box {
                    max-width: 550px; padding: 40px;
                    background: #0d1320;
                    border: 1px solid #202938;
                    border-radius: 12px;
                }
                h1 { margin-top: 0; }
                p { color: #aab2c0; line-height: 1.6; }
                a {
                    display: inline-block; margin-top: 20px;
                    color: #00d9ff; text-decoration: none;
                }
            </style>
        </head>
        <body>
            <div class="box">
                <h1>Story Submitted</h1>
                <p>Thank you for sharing your story.</p>
                <p>Your submission has been received and will be reviewed before publication.</p>
                <a href="/stories">&#8592; Back to Stories</a>
            </div>
        </body>
        </html>
        """

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

    stories_this_month = Story.query.filter(
        Story.published == True,
        db.extract("month", Story.created_at) == now.month,
        db.extract("year", Story.created_at) == now.year
    ).count()

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
        pending_submissions=pending_count
    )


# ============================================================
# CREATE STORY
# ============================================================

@app.route("/admin/create-story", methods=["GET", "POST"])
@admin_required
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
            published=published,
            read_count=0
        )

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
        Story.created_at.desc()
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
def approve_story(story_id):

    story = Story.query.get_or_404(story_id)
    story.published = True
    db.session.commit()

    return redirect(url_for("dashboard"))


# ============================================================
# EDIT STORY
# ============================================================

@app.route("/admin/stories/edit/<int:story_id>", methods=["GET", "POST"])
@admin_required
def edit_story(story_id):

    story = Story.query.get_or_404(story_id)

    if request.method == "POST":

        title = clean_text(request.form.get("title"), TITLE_MAX)
        category = clean_category(request.form.get("category"))
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
def toggle_story(story_id):

    story = Story.query.get_or_404(story_id)
    story.published = not story.published
    db.session.commit()

    return redirect(url_for("manage_stories"))


# ============================================================
# ADMIN — DELETE COMMENT
# ============================================================

@app.route("/admin/comments/delete/<int:comment_id>", methods=["POST"])
@admin_required
def delete_comment(comment_id):

    comment = Comment.query.get_or_404(comment_id)
    db.session.delete(comment)
    db.session.commit()

    return redirect(request.referrer or url_for("dashboard"))


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
        "excerpt": make_excerpt(story.content, excerpt_chars),
        "reading_minutes": reading_minutes(story.content),
        "url": url_for("view_story", slug=story.slug, _external=True),
        "cover_image": cover,
        "published_at": story.created_at.isoformat() + "Z",
        "updated_at": (story.updated_at or story.created_at).isoformat() + "Z",
    }


def feed_query():
    return Story.query.filter(Story.published == True, Story.category != "18+")


@app.route("/api/stories")
def api_stories():

    if not feed_authorized(request.headers):
        return jsonify(error="unauthorized"), 401

    if not feed_rate_ok(request.headers):
        return jsonify(error="too many requests"), 429

    limit = min(max(request.args.get("limit", 50, type=int), 1), 100)
    page = max(request.args.get("page", 1, type=int), 1)
    excerpt_chars = min(max(request.args.get("excerpt", 240, type=int), 100), 600)

    pagination = feed_query().order_by(
        Story.updated_at.desc(), Story.id.desc()
    ).paginate(page=page, per_page=limit, error_out=False)

    response = jsonify({
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
    category = clean_category(str(data.get("category") or ""))
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

    rows = feed_query().order_by(Story.updated_at.desc()).limit(5000).all()
    items = ["<url><loc>%s</loc></url>" % escape(
        url_for("stories", _external=True))]
    for story in rows:
        stamp = (story.updated_at or story.created_at).strftime("%Y-%m-%d")
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
# CREATE DATABASE TABLES (and the indexes that keep the site fast)
# ============================================================

with app.app_context():
    db.create_all()

    for statement in (
        "ALTER TABLE stories ADD COLUMN IF NOT EXISTS verified_tier VARCHAR(16)",
        "ALTER TABLE stories ADD COLUMN IF NOT EXISTS show_mark BOOLEAN DEFAULT TRUE",
        "ALTER TABLE stories ADD COLUMN verified_tier VARCHAR(16)",  # SQLite; an error on Postgres is ignored
        "ALTER TABLE stories ADD COLUMN show_mark BOOLEAN DEFAULT 1",
        "CREATE INDEX IF NOT EXISTS ix_stories_pub_created ON stories (published, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_story_reads_story ON story_reads (story_id, ip_address)",
        "CREATE INDEX IF NOT EXISTS ix_reactions_story ON reactions (story_id, ip_address)",
        "CREATE INDEX IF NOT EXISTS ix_comments_story ON comments (story_id, approved)",
    ):
        try:
            db.session.execute(db.text(statement))
            db.session.commit()
        except Exception:
            db.session.rollback()


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":

    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"

    app.run(host="0.0.0.0", port=port, debug=debug_mode)