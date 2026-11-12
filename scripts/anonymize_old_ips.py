"""ONE-TIME, OPTIONAL: replace the raw IP addresses already stored (reads,
reactions, comments) with a keyed hash, so no real IP stays in the database.

    python scripts/anonymize_old_ips.py            shows what it WOULD change
    python scripts/anonymize_old_ips.py --apply    really changes it

Run it where DATABASE_URL and SECRET_KEY are set (Render -> Shell). Running it
twice is safe: values that are already hashes are left alone.
"""
import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, Comment, Reaction, StoryRead  # noqa: E402


def keyed(ip: str) -> str:
    return hmac.new(app.secret_key.encode("utf-8"), f"ip:{ip}".encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def looks_like_ip(value) -> bool:
    return bool(value) and ("." in value or ":" in value) and len(value) <= 45


apply = "--apply" in sys.argv
with app.app_context():
    for model in (StoryRead, Reaction, Comment):
        rows = [r for r in model.query.filter(model.ip_address.isnot(None)).all()
                if looks_like_ip(r.ip_address)]
        print(f"{model.__tablename__}: {len(rows)} rows hold a raw IP address")
        if apply:
            for r in rows:
                r.ip_address = keyed(r.ip_address)
    if apply:
        db.session.commit()
        print("Done. No raw IP addresses are left.")
    else:
        print("Nothing changed. Add --apply to really do it.")
