from django.apps import AppConfig


class CryptoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crypto'

    def ready(self):
        from . import signals  # noqa: F401
