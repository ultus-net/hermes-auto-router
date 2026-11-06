"""hermes-auto-router — Synthetic primary, OpenRouter Auto Router failover.

Two capabilities ride in this plugin:

1. **Auto Router constraint** (this package). ``llm_request`` middleware resolves
   the configured ``~<lab>/<model>-latest`` aliases to the concrete slugs they
   currently point at and injects them as ``plugins[auto-router].allowed_models``
   on every ``openrouter/auto`` request. Without it the aliases match nothing
   inside ``allowed_models`` and the router 404s.

2. **Synthetic provider** (``providers/synthetic/``). A model-provider plugin
   that registers Synthetic's permanent ``syn:*`` category aliases, which are
   absent from models.dev and therefore invisible to Hermes otherwise.

Cross-provider failover is Hermes' own ``fallback_providers`` chain, written by
``hermes auto-router apply``. A plugin cannot redirect an in-flight request, and
Hermes already owns retry, cooldown, and per-turn restoration, so re-implementing
that here would fight the runtime.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from .auto_router import catalog
    from .auto_router.cli import handle, register_cli
    from .auto_router.middleware import configure, on_llm_request
    from .auto_router.settings import from_mapping

    raw = {
        "aliases": ctx.get_config("aliases", None),
        "cost_tier": ctx.get_config("cost_tier", None),
        "enabled": ctx.get_config("enabled", True),
        "primary_model": ctx.get_config("primary_model", None),
        "small_model": ctx.get_config("small_model", None),
    }
    settings = from_mapping({key: value for key, value in raw.items() if value is not None})
    configure(settings)

    ctx.register_middleware("llm_request", on_llm_request)
    ctx.register_cli_command(
        "auto-router",
        "Synthetic primary + OpenRouter Auto Router failover",
        register_cli,
        handle,
        description=(
            "Route Hermes through Synthetic's permanent syn:* aliases and fail over to "
            "OpenRouter's Auto Router, constrained to the ~latest aliases resolved to "
            "concrete slugs. See: hermes auto-router status"
        ),
    )

    if settings.enabled:
        # Warm the pool off the request path so the first openrouter/auto call is
        # already constrained. A cold cache degrades to an unconstrained router,
        # never to a failed request.
        threading.Thread(
            target=catalog.warm,
            args=(settings.aliases,),
            name="auto-router-warm",
            daemon=True,
        ).start()
