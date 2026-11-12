# Stories site - Stage 1 (security + broken features + feed for Mkuu)

## BEFORE you push: Render -> your stories service -> Environment
The site now REFUSES TO START (on purpose) unless these exist:
  SECRET_KEY        48+ random characters
  ADMIN_USERNAME    your admin name
  ADMIN_PASSWORD    a long password (or ADMIN_PASSWORD_HASH instead)
Also check:
  DATABASE_URL      must exist (Postgres). Without it the site uses the
                    stories.db FILE, which is ERASED on every deploy/restart.
Add for Mkuu (used in the next stage):
  FEED_TOKEN        24+ random characters
Make a random value in PowerShell:
  -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_})

## Steps (PowerShell, in C:\Users\Bradl\Dev\Neckrone8x-Stories-Backend)
  Expand-Archive "$HOME\Downloads\stories_stage1.zip" -DestinationPath . -Force
  .venv\Scripts\python -m unittest discover -s tests
      -> last line must say OK
  git add app.py site_security.py templates tests scripts README_STAGE1.md
  git commit -m "Stories stage 1: security, fixes, story feed"
  git push

## After it deploys, check
  /stories                 opens as before
  /admin/login             log in; Create Story: "Publish immediately" is ticked
  /robots.txt  /sitemap.xml  /health      open
  /api/stories             must say {"error":"unauthorized"} (FEED_TOKEN is working)
  a story in "18+"         the list shows no text for it

## Optional, once: remove raw IP addresses already stored
  Render -> Shell:  python scripts/anonymize_old_ips.py          (preview)
                    python scripts/anonymize_old_ips.py --apply
