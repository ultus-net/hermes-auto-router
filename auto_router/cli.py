"""``hermes auto-router`` — status, apply, verify.

``apply`` writes the routing policy through Hermes' own config writer
(``hermes_cli.config.set_config_value``), never by editing YAML by hand, so the
live gateway's config guards and atomic write still apply.

``verify`` is the honest check: it resolves the live catalogs and, when the keys
are present, sends one real completion to each provider and prints the model
that actually answered.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any

from . import catalog
from .pool import AUTO_ROUTER_MODEL
from .settings import (
    COST_TIERS,
    DEFAULT_ALIASES,
    DEFAULT_PRIMARY_MODEL,
    DEFAULT_SMALL_MODEL,
    Settings,
)

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


# ── argparse ────────────────────────────────────────────────────────────────


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="auto_router_command")

    status = subs.add_parser("status", help="Show the routing policy and the live pool")
    status.add_argument("--refresh", action="store_true", help="Bypass the catalog cache")
    status.set_defaults(func=_status)

    apply_cmd = subs.add_parser(
        "apply", help="Make Synthetic primary and OpenRouter Auto Router the fallback"
    )
    apply_cmd.add_argument("--primary", default=None, help=f"Primary syn:* alias (default {DEFAULT_PRIMARY_MODEL})")
    apply_cmd.add_argument("--small", default=None, help=f"Small/aux syn:* alias (default {DEFAULT_SMALL_MODEL})")
    apply_cmd.add_argument("--no-default", action="store_true", help="Do not change model.provider/model.default")
    apply_cmd.add_argument("--dry-run", action="store_true", help="Print the writes without applying them")
    apply_cmd.set_defaults(func=_apply)

    verify = subs.add_parser("verify", help="Live-probe both providers end to end")
    verify.add_argument("--refresh", action="store_true", help="Bypass the catalog cache")
    verify.add_argument("--timeout", type=float, default=45.0, help="Per-request timeout in seconds")
    verify.set_defaults(func=_verify)


def handle(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None:
        print("usage: hermes auto-router {status,apply,verify}", file=sys.stderr)
        return 2
    return int(func(args) or 0)


# ── helpers ─────────────────────────────────────────────────────────────────


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
    except Exception:
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
    except Exception:
        pass
    return out or dict(FALLBACK_CAPABILITIES)


def _yaml(value: Any) -> str:
    import yaml

    return yaml.safe_dump(value, sort_keys=False, default_flow_style=True).strip()


def _post_chat(
    base_url: str, api_key: str, model: str, *, extra: dict[str, Any] | None = None, timeout: float = 45.0
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "max_tokens": 16,
    }
    if extra:
        body.update(extra)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _reply_text(payload: dict[str, Any]) -> str:
    try:
        return (payload["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""


# ── status ──────────────────────────────────────────────────────────────────


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
        aliases = catalog.synthetic_aliases()
        print(f"synthetic      {len(aliases)} syn:* aliases live")
        for alias in aliases:
            print(f"  {alias}")
    except Exception as exc:
        print(f"synthetic      catalog unreachable: {exc}")
    print()

    try:
        pool = catalog.resolved_pool(settings.aliases, refresh=args.refresh)
        print(f"openrouter     {len(pool)}/{len(settings.aliases)} aliases resolve")
        for slug in pool:
            print(f"  {slug}")
        missing = [a for a in settings.aliases if a not in catalog.alias_targets(catalog.openrouter_models())]
        for alias in missing:
            print(f"  (no live target) {alias}")
    except Exception as exc:
        print(f"openrouter     catalog unreachable: {exc}")
    return 0


# ── apply ───────────────────────────────────────────────────────────────────


def _apply(args: argparse.Namespace) -> int:
    from hermes_cli.config import set_config_value

    primary = args.primary or DEFAULT_PRIMARY_MODEL
    small = args.small or DEFAULT_SMALL_MODEL
    if not primary.startswith("syn:") or not small.startswith("syn:"):
        print("✗ --primary and --small must be syn:* aliases", file=sys.stderr)
        return 2

    writes: list[tuple[str, str, bool]] = []
    if not args.no_default:
        writes += [
            ("model.provider", "synthetic", False),
            ("model.default", primary, False),
            ("model.base_url", "", False),
            ("model_aliases.syn:auto", _yaml({"model": primary, "provider": "synthetic"}), True),
        ]
    writes += [
        ("fallback_providers", _yaml([{"provider": "openrouter", "model": AUTO_ROUTER_MODEL}]), False),
        # force: the validator only knows the top-level `model_overrides` key, not
        # per-provider children, and would warn on a key the runtime does read.
        ("model_overrides.synthetic", _yaml(_capabilities()), True),
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


# ── verify ──────────────────────────────────────────────────────────────────


def _verify(args: argparse.Namespace) -> int:
    settings = _settings()
    failures = 0

    print("1. Synthetic catalog")
    try:
        aliases = catalog.synthetic_aliases()
        print(f"   ✓ {len(aliases)} syn:* aliases: {', '.join(aliases) or '(none)'}")
    except Exception as exc:
        print(f"   ✗ unreachable: {exc}")
        aliases = []
        failures += 1

    print("2. OpenRouter alias resolution")
    try:
        targets = catalog.alias_targets(catalog.openrouter_models())
        pool = catalog.resolved_pool(settings.aliases, refresh=args.refresh)
        print(f"   ✓ {len(pool)}/{len(settings.aliases)} aliases resolve to concrete slugs")
        for slug in pool:
            print(f"     {slug}")
        if not pool:
            failures += 1
    except Exception as exc:
        print(f"   ✗ unreachable: {exc}")
        pool = []
        failures += 1

    print("3. Synthetic completion")
    synthetic_key = os.environ.get("SYNTHETIC_API_KEY", "").strip()
    if not synthetic_key:
        print("   - skipped: SYNTHETIC_API_KEY is not set")
    else:
        model = settings.primary_model
        try:
            payload = _post_chat(SYNTHETIC_BASE_URL, synthetic_key, model, timeout=args.timeout)
            print(f"   ✓ {model} answered via {payload.get('model', '?')}: {_reply_text(payload)!r}")
        except Exception as exc:
            print(f"   ✗ {model} failed: {exc}")
            failures += 1

    print("4. OpenRouter Auto Router completion")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not openrouter_key:
        print("   - skipped: OPENROUTER_API_KEY is not set")
    elif not pool:
        print("   - skipped: no resolved pool to constrain the router with")
    else:
        try:
            payload = _post_chat(
                OPENROUTER_BASE_URL,
                openrouter_key,
                AUTO_ROUTER_MODEL,
                extra={"plugins": [{"id": "auto-router", "allowed_models": pool}]},
                timeout=args.timeout,
            )
            print(f"   ✓ {AUTO_ROUTER_MODEL} chose {payload.get('model', '?')}: {_reply_text(payload)!r}")
        except Exception as exc:
            print(f"   ✗ {AUTO_ROUTER_MODEL} failed: {exc}")
            failures += 1

    print()
    print("✓ all checks passed" if failures == 0 else f"✗ {failures} check(s) failed")
    return 0 if failures == 0 else 1
