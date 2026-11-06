"""``llm_request`` middleware: constrain ``openrouter/auto`` to the resolved pool.

Hermes builds the provider kwargs, then hands them to ``llm_request`` middleware
before the call. That is the one place a plugin can shape an outbound request,
so it is where the Auto Router's ``allowed_models`` constraint is attached.

The middleware is deliberately narrow:

* only ``provider == openrouter`` and ``model == openrouter/auto``;
* only when the base URL is actually OpenRouter (a custom proxy that happens to
  be named ``openrouter`` must not receive the field);
* only when the pool resolves to at least one concrete slug.

Anything else returns ``None``, which means "no change".
"""

from __future__ import annotations

import logging
from typing import Any

from . import catalog
from .pool import AUTO_ROUTER_MODEL, inject
from .settings import Settings

logger = logging.getLogger(__name__)

_settings = Settings()


def configure(settings: Settings) -> None:
    global _settings
    _settings = settings


def current_settings() -> Settings:
    return _settings


def _is_openrouter(base_url: Any) -> bool:
    return isinstance(base_url, str) and "openrouter.ai" in base_url.lower()


def on_llm_request(**kwargs: Any) -> dict[str, Any] | None:
    if not _settings.enabled:
        return None
    if (kwargs.get("provider") or "").strip().lower() != "openrouter":
        return None
    if (kwargs.get("model") or "").strip() != AUTO_ROUTER_MODEL:
        return None
    if not _is_openrouter(kwargs.get("base_url")):
        return None
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    try:
        pool = catalog.resolved_pool(_settings.aliases)
    except Exception as exc:
        logger.debug("auto-router: pool resolution failed: %s", exc)
        return None
    if not pool:
        return None
    return {
        "request": inject(request, pool, _settings.cost_tier),
        "source": "auto-router",
        "reason": f"allowed_models={len(pool)}",
    }
