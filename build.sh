#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

# SITE-0: no `migrate`. The public site no longer uses a database, and the
# Render Postgres must not be repaired, recreated or written to.
