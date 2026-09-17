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
