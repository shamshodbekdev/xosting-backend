import os

from django.core.wsgi import get_wsgi_application
from xosting_shared.tracing import instrument_django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
instrument_django("billing-service")
application = get_wsgi_application()
