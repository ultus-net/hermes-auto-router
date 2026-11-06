"""Synthetic provider profile — permanent ``syn:*`` category aliases.

Synthetic publishes category aliases (``syn:large:text``, ``syn:small:text``,
``syn:large:vision``, ``syn:small:vision``) that always point at the current best
model for that category. They are absent from the public models.dev catalog, so
Hermes cannot select them out of the box: this profile discovers them from
Synthetic's OpenAI-compatible ``/models`` endpoint and exposes them as the
provider's catalog.

Only ``syn:*`` ids are kept. Concrete ``hf:*`` / ``infinite:*`` ids are ignored
on purpose: the point is to never pin a version by hand.

The endpoint is public, so discovery works before a key is configured. Inference
still needs ``SYNTHETIC_API_KEY``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

MODELS_URL = "https://api.synthetic.new/v1/models"
ALIAS_PREFIX = "syn:"
CACHE_TTL_SECONDS = 6 * 60 * 60

# Shown by the picker when the live catalog is unreachable. These are the four
# aliases Synthetic has published since the alias scheme launched.
FALLBACK_ALIASES: tuple[str, ...] = (
    "syn:large:text",
    "syn:small:text",
    "syn:large:vision",
    "syn:small:vision",
)

_cache: tuple[float, list[str]] | None = None


def parse_aliases(payload: Any) -> list[str]:
    """Reduce a ``/models`` payload to its ``syn:*`` ids, in catalog order."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    aliases: list[str] = []
    for model in data or ():
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if (
            isinstance(model_id, str)
            and model_id.startswith(ALIAS_PREFIX)
            and model_id not in aliases
        ):
            aliases.append(model_id)
    return aliases


class SyntheticProfile(ProviderProfile):
    """Synthetic — OpenAI-compatible endpoint fronting rotating category aliases."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Live ``syn:*`` catalog. Public endpoint, so no auth is required.

        Returns ``None`` on failure so the caller falls back to
        ``fallback_models``; a stale in-process cache is preferred over ``None``.
        """
        global _cache  # noqa: PLW0603
        now = time.time()
        if _cache and now - _cache[0] < CACHE_TTL_SECONDS:
            return list(_cache[1])
        try:
            request = urllib.request.Request(
                MODELS_URL,
                headers={"Accept": "application/json", "User-Agent": "hermes-cli"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                aliases = parse_aliases(json.loads(response.read().decode("utf-8")))
        except Exception as exc:
            logger.debug("fetch_models(synthetic): %s", exc)
            return list(_cache[1]) if _cache else None
        if not aliases:
            logger.warning("Synthetic catalog returned no syn:* aliases")
            return list(_cache[1]) if _cache else None
        _cache = (now, aliases)
        return list(aliases)

    def resolve_aux_model(self, *, vision: bool = False) -> str:
        """Cheap alias for auxiliary work, tracking the vision split."""
        return "syn:small:vision" if vision else "syn:small:text"


synthetic = SyntheticProfile(
    name="synthetic",
    aliases=("syn",),
    display_name="Synthetic",
    description="Synthetic — permanent syn:* category aliases (auto-rotating models)",
    signup_url="https://synthetic.new",
    env_vars=("SYNTHETIC_API_KEY", "SYNTHETIC_BASE_URL"),
    base_url="https://api.synthetic.new/v1",
    models_url=MODELS_URL,
    auth_type="api_key",
    supports_vision=True,
    fallback_models=FALLBACK_ALIASES,
    default_aux_model="syn:small:text",
)

register_provider(synthetic)
