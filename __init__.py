"""hermes-auto-router — Synthetic primary, OpenRouter Auto Router failover.

Three capabilities ride in this plugin:

1. **Auto Router constraint** (``auto_router/``). ``llm_request`` middleware
   resolves the configured ``~<lab>/<model>-latest`` aliases to the concrete
   slugs they currently point at and injects them as
   ``plugins[auto-router].allowed_models`` on every ``openrouter/auto`` request.
   Without it the aliases match nothing inside ``allowed_models`` and the router
   404s.

2. **Synthetic provider** (``providers/synthetic/``). A model-provider plugin
   that registers Synthetic's permanent ``syn:*`` category aliases, which are
   absent from models.dev and therefore invisible to Hermes otherwise.

3. **Mail organiser** (``mail_organiser/``). The durable mail seam: a journalled
   state machine, a deterministic rule engine, the policy gate, the Gmail
   adapter, and the taxonomy bootstrap. It registers no command or tool yet.

Cross-provider failover is Hermes' own ``fallback_providers`` chain, written by
``hermes auto-router apply``. A plugin cannot redirect an in-flight request, and
Hermes already owns retry, cooldown, and per-turn restoration, so re-implementing
that here would fight the runtime.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

#: The plugin id in log lines. The D11 rename changes it with the manifest.
PLUGIN_ID = "auto-router"


def register(ctx) -> None:
    """Register every seam behind its own guard, routing first.

    A session with no routing is broken and a session with no mail is not, so
    the router registers first. A mail failure logs with the plugin id and
    disables mail only; Hermes disposes every registration when ``register()``
    itself raises.
    """
    _register_router(ctx)
    try:
        from .mail_organiser import register as register_mail

        register_mail(ctx)
    except Exception:
        logger.exception("%s: mail seam unavailable; routing stays registered", PLUGIN_ID)


def _register_router(ctx) -> None:
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
        # Warm the target map off the request path so the first openrouter/auto
        # call is already constrained. A cold cache degrades to an
        # unconstrained router, never to a failed request.
        threading.Thread(
            target=catalog.warm,
            name="auto-router-warm",
            daemon=True,
        ).start()
