import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# Railway auto-deploy test
class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'
    verbose_name = 'Accounts'

    def ready(self):
        # Runs once per worker process on startup. Logs which commit is
        # actually live so a stale Railway deploy is visible in the logs
        # immediately, without needing dashboard access.
        from .version import current_build_info

        info = current_build_info()
        logger.warning(
            'BOOT commit=%s branch=%s deployment=%s env=%s',
            info['commit_short'],
            info['branch'],
            info['deployment_id'],
            info['environment'],
        )
        self._warn_missing_provider_config()

    def _warn_missing_provider_config(self):
        # These are the credentials that, when blank, don't fail at boot —
        # they fail silently until the first real request hits that
        # provider (e.g. a blank RELOADLY_CLIENT_ID surfaces only as a 502
        # "Bad Gateway" on /giftcards/brands/, with the real cause buried in
        # a request log). Flagging them here puts the cause next to the
        # commit line, visible on every deploy without waiting for a user
        # to hit the broken endpoint.
        from django.conf import settings

        required = {
            'RELOADLY_CLIENT_ID': settings.RELOADLY_CLIENT_ID,
            'RELOADLY_CLIENT_SECRET': settings.RELOADLY_CLIENT_SECRET,
            'QUIDAX_SECRET_KEY': settings.QUIDAX_SECRET_KEY,
            'DOJAH_SECRET_KEY': settings.DOJAH_SECRET_KEY,
            'BITNOB_CLIENT_ID': settings.BITNOB_CLIENT_ID,
            'BITNOB_CLIENT_SECRET': settings.BITNOB_CLIENT_SECRET,
            'FLW_SECRET_KEY': settings.FLW_SECRET_KEY,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            logger.warning(
                'BOOT missing_provider_config=%s — these features will '
                'return 502s at request time until set in the environment.',
                ', '.join(missing),
            )
