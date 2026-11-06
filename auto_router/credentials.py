"""Credential lookup for the live CLI probes.

Hermes keeps provider keys in ``$HERMES_HOME/.env``, not in the shell that
launched ``hermes``. Resolve them through Hermes' own dotenv-aware lookup when
it is importable, so ``hermes auto-router verify`` uses the same key the runtime
uses. A bare checkout without Hermes falls back to ``os.environ``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def api_key(name: str) -> str:
    """Resolve one credential by env-var name, preferring ``$HERMES_HOME/.env``."""
    try:
        from agent.credential_pool import get_env_prefer_dotenv
    except Exception as exc:
        logger.debug("auto-router: Hermes credential resolver unavailable: %s", exc)
        return os.environ.get(name, "").strip()
    try:
        return (get_env_prefer_dotenv(name) or "").strip()
    except Exception as exc:
        logger.debug("auto-router: credential lookup failed for %s: %s", name, exc)
        return os.environ.get(name, "").strip()
