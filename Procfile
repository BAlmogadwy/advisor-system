release: python manage.py migrate --noinput && python manage.py createcachetable --database default && python manage.py purge_rate_limit_buckets
web: gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
