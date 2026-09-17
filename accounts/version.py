"""Deploy diagnostics — confirms which build is actually serving traffic.

Railway injects RAILWAY_GIT_COMMIT_SHA / RAILWAY_GIT_BRANCH /
RAILWAY_DEPLOYMENT_ID automatically into every deploy; no manual config
needed. Falls back to `git rev-parse` for local runs outside Railway.
"""

from __future__ import annotations

import logging
import os
import subprocess

from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


def _local_git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
    except Exception:
        return None


def current_build_info() -> dict:
    commit = os.environ.get('RAILWAY_GIT_COMMIT_SHA') or _local_git_sha() or 'unknown'
    return {
        'commit': commit,
        'commit_short': commit[:7] if commit != 'unknown' else commit,
        'branch': os.environ.get('RAILWAY_GIT_BRANCH', 'local'),
        'deployment_id': os.environ.get('RAILWAY_DEPLOYMENT_ID', 'local'),
        'environment': os.environ.get('RAILWAY_ENVIRONMENT_NAME', 'local'),
    }


class VersionView(APIView):
    """GET /api/version/ — which commit is actually running right now."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response(current_build_info())
