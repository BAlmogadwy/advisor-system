release: python manage.py migrate --noinput && python manage.py createcachetable --database default
web: gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
