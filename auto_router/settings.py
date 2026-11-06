"""Plugin settings, read once at load from ``plugins.entries.auto-router.settings``.

Malformed values fall back to defaults rather than raising: a bad option should
degrade the routing policy, not break the session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# Synthetic's permanent category aliases. The alias id is stable; Synthetic
# rotates the model behind it, so nothing here pins a version.
DEFAULT_PRIMARY_MODEL = "syn:large:text"
DEFAULT_SMALL_MODEL = "syn:small:text"

# Frontier pool fed to the Auto Router. Any alias without a live target is
# skipped at resolution time, so this list is intentionally tolerant of models
# that do not exist yet.
DEFAULT_ALIASES: tuple[str, ...] = (
    "~anthropic/claude-opus-latest",
    "~anthropic/claude-sonnet-latest",
    "~anthropic/claude-haiku-latest",
    "~anthropic/claude-fable-latest",
    "~openai/gpt-astra-latest",
    "~openai/gpt-sol-latest",
    "~openai/gpt-terra-latest",
    "~openai/gpt-luna-latest",
    "~openai/gpt-mini-latest",
    "~google/gemini-pro-latest",
    "~google/gemini-flash-latest",
    "~x-ai/grok-latest",
    "~deepseek/deepseek-pro-latest",
    "~deepseek/deepseek-flash-latest",
    "~deepseek/deepseek-v4-flash-latest",
    "~z-ai/glm-latest",
    "~z-ai/glm-flash-latest",
    "~moonshotai/kimi-latest",
)

COST_TIERS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Settings:
    aliases: tuple[str, ...] = DEFAULT_ALIASES
    cost_tier: str | None = None
    enabled: bool = True
    primary_model: str = DEFAULT_PRIMARY_MODEL
    small_model: str = DEFAULT_SMALL_MODEL


def _aliases(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return DEFAULT_ALIASES
    kept = tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip().startswith("~")
    )
    return kept or DEFAULT_ALIASES


def _syn_alias(value: Any, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip().startswith("syn:") else fallback


def from_mapping(raw: Mapping[str, Any] | None) -> Settings:
    raw = raw or {}
    tier = raw.get("cost_tier")
    return Settings(
        aliases=_aliases(raw.get("aliases")),
        cost_tier=tier if isinstance(tier, str) and tier in COST_TIERS else None,
        enabled=raw.get("enabled") is not False,
        primary_model=_syn_alias(raw.get("primary_model"), DEFAULT_PRIMARY_MODEL),
        small_model=_syn_alias(raw.get("small_model"), DEFAULT_SMALL_MODEL),
    )
