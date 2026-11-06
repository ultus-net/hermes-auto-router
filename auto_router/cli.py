"""``hermes auto-router`` — status, apply, verify.

``apply`` writes the routing policy through Hermes' own config writer
(``hermes_cli.config.set_config_value``), never by editing YAML by hand, so the
live gateway's config guards and atomic write still apply. Structured values are
serialised as JSON: Hermes parses YAML and JSON is a YAML subset, so the plugin
does not need a PyYAML dependency of its own.

``verify`` is the honest check: it resolves the live catalogs and, when the keys
are present, sends one real completion to each provider and prints the model
that actually answered.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from typing import Any

from . import catalog
from .credentials import api_key
from .pool import AUTO_ROUTER_MODEL, auto_router_entry, resolve_pool
from .settings import Settings

logger = logging.getLogger(__name__)

SYNTHETIC_BASE_URL = "https://api.synthetic.new/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Used only when the live Synthetic catalog is unreachable during ``apply``.
FALLBACK_CAPABILITIES: dict[str, dict[str, Any]] = {
    "syn:large:text": {
        "context_window": 524288,
        "supports_tools": True,
        "supports_vision": True,
        "supports_reasoning": True,
        "model_family": "synthetic",
    },
    "syn:small:text": {
        "context_window": 196608,
        "supports_tools": True,
        "supports_vision": False,
        "supports_reasoning": True,
        "model_family": "synthetic",
    },
    "syn:large:vision": {
        "context_window": 524288,
        "supports_tools": True,
        "supports_vision": True,
        "supports_reasoning": True,
        "model_family": "synthetic",
    },
    "syn:small:vision": {
        "context_window": 524288,
        "supports_tools": True,
        "supports_vision": True,
        "supports_reasoning": True,
        "model_family": "synthetic",
    },
}


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="auto_router_command")

    status = subs.add_parser("status", help="Show the routing policy and the live pool")
    status.add_argument("--refresh", action="store_true", help="Bypass the catalog caches")
    status.set_defaults(func=_status)

    apply_cmd = subs.add_parser(
        "apply", help="Make Synthetic primary and OpenRouter Auto Router the fallback"
    )
    apply_cmd.add_argument(
        "--primary",
        default=None,
        help="Primary syn:* alias (default: the configured primary_model)",
    )
    apply_cmd.add_argument(
        "--small",
        default=None,
        help="Small/aux syn:* alias (default: the configured small_model)",
    )
    apply_cmd.add_argument(
        "--no-default",
        action="store_true",
        help=(
            "Do not change model.provider, model.default, model.base_url, "
            "or model_aliases.syn:auto"
        ),
    )
    apply_cmd.add_argument(
        "--dry-run", action="store_true", help="Print the writes without applying them"
    )
    apply_cmd.set_defaults(func=_apply)

    verify = subs.add_parser("verify", help="Live-probe both providers end to end")
    verify.add_argument("--refresh", action="store_true", help="Bypass the catalog caches")
    verify.add_argument(
        "--timeout", type=float, default=45.0, help="Per-request timeout in seconds"
    )
    verify.set_defaults(func=_verify)


def handle(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None:
        print("usage: hermes auto-router {status,apply,verify}", file=sys.stderr)
        return 2
    return int(func(args) or 0)


def _settings() -> Settings:
    from .middleware import current_settings

    return current_settings()


def _config_get(key: str, default: Any = None) -> Any:
    try:
        from hermes_cli.config import load_config

        node: Any = load_config()
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node
    except Exception as exc:
        logger.debug("auto-router: config read failed for %s: %s", key, exc)
        return default


def _capabilities() -> dict[str, dict[str, Any]]:
    """Per-model metadata for the ``syn:*`` aliases, derived from the live catalog.

    Hermes resolves capabilities from models.dev, which does not know Synthetic's
    aliases. ``model_overrides`` is the supported place to declare them, and
    deriving the values from the catalog keeps them current instead of pinned.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        for model in catalog.synthetic_models():
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.startswith("syn:"):
                continue
            features = model.get("supported_features") or []
            modalities = model.get("input_modalities") or []
            efforts = (model.get("reasoning_parameters") or {}).get("efforts") or []
            entry: dict[str, Any] = {
                "supports_tools": "tools" in features,
                "supports_vision": "image" in modalities,
                "supports_reasoning": bool(efforts),
                "model_family": "synthetic",
            }
            context = model.get("context_length")
            if isinstance(context, int) and context > 0:
                entry["context_window"] = context
            out[model_id] = entry
    except Exception as exc:
        logger.warning(
            "auto-router: live capability discovery failed (%s); using built-in defaults",
            exc,
        )
    return out or dict(FALLBACK_CAPABILITIES)


def _structured(value: Any) -> str:
    """Serialise a list or mapping for ``set_config_value``.

    Hermes parses the value as YAML, and JSON is valid YAML. That keeps this
    plugin dependency-free while still producing real dicts and lists.
    """
    return json.dumps(value)


def _post_chat(
    base_url: str,
    api_key_value: str,
    model: str,
    *,
    extra: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "max_tokens": 64,
    }
    if extra:
        body.update(extra)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key_value}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _reply_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"].get("content")
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        logger.debug("auto-router: unexpected completion payload: %s", exc)
        return ""
    return content.strip() if isinstance(content, str) else ""


def _status(args: argparse.Namespace) -> int:
    settings = _settings()
    print("auto-router")
    print(f"  enabled        {settings.enabled}")
    print(f"  primary        {settings.primary_model}")
    print(f"  small          {settings.small_model}")
    print(f"  cost_tier      {settings.cost_tier or '(account default)'}")
    print(f"  aliases        {len(settings.aliases)} configured")
    print()
    print("config")
    print(f"  model.provider {_config_get('model.provider', '') or '(unset)'}")
    print(f"  model.default  {_config_get('model.default', '') or '(unset)'}")
    fallback = _config_get("fallback_providers", []) or []
    if fallback:
        for entry in fallback:
            if isinstance(entry, dict):
                print(f"  fallback       {entry.get('provider')}/{entry.get('model')}")
    else:
        print("  fallback       (none)")
    print()

    try:
        aliases = catalog.synthetic_aliases(refresh=args.refresh)
        print(f"synthetic      {len(aliases)} syn:* aliases live")
        for alias in aliases:
            print(f"  {alias}")
    except Exception as exc:
        print(f"synthetic      catalog unreachable: {exc}")
    print()

    try:
        targets = catalog.alias_target_map(refresh=args.refresh)
        pool = resolve_pool(settings.aliases, targets)
        print(f"openrouter     {len(pool)}/{len(settings.aliases)} aliases resolve")
        for slug in pool:
            print(f"  {slug}")
        for alias in settings.aliases:
            if alias not in targets:
                print(f"  (no live target) {alias}")
    except Exception as exc:
        print(f"openrouter     catalog unreachable: {exc}")
    return 0


def _apply(args: argparse.Namespace) -> int:
    from hermes_cli.config import set_config_value

    settings = _settings()
    primary = args.primary or settings.primary_model
    small = args.small or settings.small_model
    if not primary.startswith("syn:") or not small.startswith("syn:"):
        print("✗ --primary and --small must be syn:* aliases", file=sys.stderr)
        return 2

    writes: list[tuple[str, str, bool]] = []
    if not args.no_default:
        writes += [
            ("model.provider", "synthetic", False),
            ("model.default", primary, False),
            ("model.base_url", "", False),
            ("model_aliases.syn:auto", _structured({"model": primary, "provider": "synthetic"}), True),
        ]
    writes += [
        (
            "fallback_providers",
            _structured([{"provider": "openrouter", "model": AUTO_ROUTER_MODEL}]),
            False,
        ),
        # force: the validator only knows the top-level `model_overrides` key, not
        # per-provider children, and would warn on a key the runtime does read.
        ("model_overrides.synthetic", _structured(_capabilities()), True),
        ("auxiliary.title_generation.provider", "synthetic", False),
        ("auxiliary.title_generation.model", small, False),
    ]

    for key, value, force in writes:
        if args.dry_run:
            print(f"  would set {key} = {value}")
            continue
        set_config_value(key, value, force=force)

    if args.dry_run:
        print("dry run: nothing written")
        return 0
    print()
    print("✓ Synthetic is primary; OpenRouter Auto Router is the fallback.")
    print("  Restart any running gateway/desktop backend to pick up the new model.")
    print("  Run `hermes auto-router verify` to prove both routes.")
    return 0


def _check_synthetic_catalog(refresh: bool) -> int:
    print("1. Synthetic catalog")
    try:
        aliases = catalog.synthetic_aliases(refresh=refresh)
    except Exception as exc:
        print(f"   ✗ unreachable: {exc}")
        return 1
    if not aliases:
        print("   ✗ no syn:* aliases published")
        return 1
    print(f"   ✓ {len(aliases)} syn:* aliases: {', '.join(aliases)}")
    return 0


def _check_pool(settings: Settings, refresh: bool) -> tuple[list[str], int]:
    print("2. OpenRouter alias resolution")
    try:
        pool = catalog.resolved_pool(settings.aliases, refresh=refresh)
    except Exception as exc:
        print(f"   ✗ unreachable: {exc}")
        return [], 1
    print(f"   ✓ {len(pool)}/{len(settings.aliases)} aliases resolve to concrete slugs")
    for slug in pool:
        print(f"     {slug}")
    return pool, 0 if pool else 1


def _check_synthetic_completion(settings: Settings, timeout: float) -> int:
    print("3. Synthetic completion")
    key = api_key("SYNTHETIC_API_KEY")
    if not key:
        print("   - skipped: SYNTHETIC_API_KEY is not set")
        return 0
    model = settings.primary_model
    try:
        payload = _post_chat(SYNTHETIC_BASE_URL, key, model, timeout=timeout)
    except Exception as exc:
        print(f"   ✗ {model} failed: {exc}")
        return 1
    print(f"   ✓ {model} answered via {payload.get('model', '?')}: {_reply_text(payload)!r}")
    return 0


def _check_openrouter_completion(settings: Settings, pool: list[str], timeout: float) -> int:
    print("4. OpenRouter Auto Router completion")
    key = api_key("OPENROUTER_API_KEY")
    if not key:
        print("   - skipped: OPENROUTER_API_KEY is not set")
        return 0
    if not pool:
        print("   - skipped: no resolved pool to constrain the router with")
        return 0
    try:
        payload = _post_chat(
            OPENROUTER_BASE_URL,
            key,
            AUTO_ROUTER_MODEL,
            extra={"plugins": [auto_router_entry(pool, settings.cost_tier)]},
            timeout=timeout,
        )
    except Exception as exc:
        print(f"   ✗ {AUTO_ROUTER_MODEL} failed: {exc}")
        return 1
    print(f"   ✓ {AUTO_ROUTER_MODEL} chose {payload.get('model', '?')}: {_reply_text(payload)!r}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    settings = _settings()
    failures = _check_synthetic_catalog(args.refresh)
    pool, pool_failures = _check_pool(settings, args.refresh)
    failures += (
        pool_failures
        + _check_synthetic_completion(settings, args.timeout)
        + _check_openrouter_completion(settings, pool, args.timeout)
    )
    print()
    print("✓ all checks passed" if failures == 0 else f"✗ {failures} check(s) failed")
    return 0 if failures == 0 else 1
