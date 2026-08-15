#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
# Render does not preserve /opt/render/.cache in the running service. Install
# Playwright browsers beside the Python package so the worker can launch the
# exact revision after the build image is promoted.
PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install chromium
python manage.py collectstatic --noinput
