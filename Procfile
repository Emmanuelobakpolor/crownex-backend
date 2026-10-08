release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
web: gunicorn crownexbackend.wsgi --bind 0.0.0.0:$PORT --timeout 60 --log-file -
worker: python manage.py process_deposit_settlements --loop
