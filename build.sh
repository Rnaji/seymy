#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

# Publication gate: fails the build (Render keeps the current deploy) while a
# public page still shows an "À FOURNIR" placeholder.
python manage.py check_public_pages

# SITE-0: no `migrate`. The public site no longer uses a database, and the
# Render Postgres must not be repaired, recreated or written to.
