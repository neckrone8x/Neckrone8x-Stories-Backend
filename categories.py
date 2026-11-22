"""The one list of story categories.

Everything that shows, checks or filters by category reads from here: the
list page chips, the Share-your-story form, the admin create/edit forms, the
admin review page, the feed API filter, the Mkuu submission check and the
tests. To add a category, add one line to CATEGORY_LIST.

Stories store the display name (e.g. "Mystery & Horror") in their category
column, as before; links use the slug (?category=mystery-horror).
"""
import re
from typing import NamedTuple, Optional


class Category(NamedTuple):
    name: str      # shown to readers and stored on the story
    slug: str      # used in links


# The one order for everything (most popular first): the Share and admin
# forms, GET /api/categories and the list page. The main chip row always shows
# the first CHIPS_SHOWN (8) of these, even with no stories; the More panel
# lists the rest, in this order. Names and slugs are stored data: never rename.
CATEGORY_LIST = [
    Category("Relationship", "relationship"),
    Category("Confession", "confession"),
    Category("Campus", "campus"),
    Category("Novels", "novels"),
    Category("Marriage", "marriage"),
    Category("Family", "family"),
    Category("Experience", "experience"),
    Category("18+", "18-plus"),       # adult: age gate, no excerpt (see app.py)
    Category("Mystery & Horror", "mystery-horror"),
    Category("Life", "life"),
    Category("News", "news"),
    Category("Politics", "politics"),
    Category("Sports", "sports"),
    Category("Technology", "technology"),
    Category("General", "general"),
    Category("Other", "other"),
]

CATEGORIES = [c.name for c in CATEGORY_LIST]
DEFAULT_CATEGORY = "General"
ADULT_CATEGORY = "18+"
CHIPS_SHOWN = 8                      # the main chip row: the first 8 above


def _key(value):
    """A forgiving comparison key: 'Mystery & Horror', 'mystery-horror',
    'MYSTERY and horror' and 'mystery_horror' all give 'mystery-horror'."""
    v = (value or "").strip().lower()
    v = re.sub(r"[^a-z0-9+]+", "-", v).strip("-")
    v = re.sub(r"(^|-)and(-|$)", "-", v).strip("-")
    return re.sub(r"-+", "-", v)


_LOOKUP = {}
for _c in CATEGORY_LIST:
    _LOOKUP[_key(_c.name)] = _c
    _LOOKUP[_c.slug] = _c


def find_category(value) -> Optional[Category]:
    """The category for a name or slug in any reasonable spelling, else None."""
    if not isinstance(value, str) or len(value) > 100:
        return None
    return _LOOKUP.get(_key(value))


def canonical_category(value):
    """The exact display name, or None if it is not one of ours."""
    found = find_category(value)
    return found.name if found else None


def clean_category(value) -> str:
    """For anything submitted: a known category, otherwise General."""
    return canonical_category(value) or DEFAULT_CATEGORY


def category_slug(name) -> str:
    """The link form of a category name (an old, unlisted name still gets one)."""
    found = find_category(name)
    return found.slug if found else _key(name)
