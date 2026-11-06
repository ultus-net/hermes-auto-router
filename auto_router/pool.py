"""Pure routing logic for the OpenRouter Auto Router.

No I/O, no host imports: everything here is a function of its arguments so the
behaviour can be tested without a network or a Hermes process.

Why this module exists
----------------------
OpenRouter's Auto Router accepts a ``plugins`` constraint::

    {"model": "openrouter/auto",
     "plugins": [{"id": "auto-router", "allowed_models": ["anthropic/claude-sonnet-4.6"]}]}

``allowed_models`` matches **concrete catalog ids only**. The ``~<lab>/<model>-latest``
aliases resolve fine as a top-level ``model``, but inside ``allowed_models`` they
match nothing, the candidate pool collapses to zero, and every request fails with
``404 No models match your request and model restrictions``.

So the alias list is resolved to the concrete slugs it currently points at
(``alias_target.slug`` in OpenRouter's public catalog) before injection.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

ALIAS_PREFIX = "~"
AUTO_ROUTER_PLUGIN_ID = "auto-router"
AUTO_ROUTER_MODEL = "openrouter/auto"


def alias_targets(models: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
    """Map every ``~...-latest`` alias to the concrete slug it resolves to.

    Entries without an ``alias_target.slug`` are skipped: an alias with no live
    target is a model that does not exist yet, and injecting it would poison the
    pool.
    """
    targets: dict[str, str] = {}
    for model in models or ():
        if not isinstance(model, Mapping):
            continue
        model_id = model.get("id")
        target = model.get("alias_target")
        slug = target.get("slug") if isinstance(target, Mapping) else None
        if (
            isinstance(model_id, str)
            and model_id.startswith(ALIAS_PREFIX)
            and isinstance(slug, str)
            and slug
        ):
            targets[model_id] = slug
    return targets


def resolve_pool(
    aliases: Sequence[str] | None, targets: Mapping[str, str]
) -> list[str]:
    """Reduce an alias list to the concrete slugs that currently resolve.

    Order follows the configured alias order (frontier models first), and
    duplicates are dropped so two aliases pointing at the same release do not
    weight the router's choice.
    """
    seen: set[str] = set()
    pool: list[str] = []
    for alias in aliases or ():
        slug = targets.get(alias)
        if slug and slug not in seen:
            seen.add(slug)
            pool.append(slug)
    return pool


def auto_router_entry(pool: Sequence[str], cost_tier: str | None = None) -> dict[str, Any]:
    """Build the ``plugins[]`` entry the Auto Router reads."""
    entry: dict[str, Any] = {"id": AUTO_ROUTER_PLUGIN_ID, "allowed_models": list(pool)}
    if cost_tier:
        entry["cost_tier"] = cost_tier
    return entry


def merge_auto_router(
    existing: Any, entry: Mapping[str, Any]
) -> list[Any]:
    """Replace any prior ``auto-router`` entry, keep every other plugin.

    A caller (or a previous middleware pass) may already have put an
    ``auto-router`` entry in the list. Keeping both would let the stale one win
    depending on server-side ordering, so the last writer replaces it.
    """
    kept = [
        plugin
        for plugin in (existing if isinstance(existing, list) else [])
        if not (isinstance(plugin, Mapping) and plugin.get("id") == AUTO_ROUTER_PLUGIN_ID)
    ]
    return [*kept, dict(entry)]


def inject(
    request: Mapping[str, Any],
    pool: Sequence[str],
    cost_tier: str | None = None,
) -> dict[str, Any]:
    """Return a copy of *request* carrying the Auto Router constraint.

    An empty pool returns the request untouched: an unconstrained Auto Router
    still works, and injecting ``allowed_models: []`` would fail every call.
    """
    out = dict(request)
    if not pool:
        return out
    extra_body = dict(out.get("extra_body") or {})
    extra_body["plugins"] = merge_auto_router(
        extra_body.get("plugins"), auto_router_entry(pool, cost_tier)
    )
    out["extra_body"] = extra_body
    return out
