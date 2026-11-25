#!/usr/bin/env bash
set -euo pipefail
python manage.py migrate --noinput
python manage.py bootstrap_admin
python manage.py collectstatic --noinput
exec gunicorn checkin_project.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 1 --threads 4 --timeout 60
