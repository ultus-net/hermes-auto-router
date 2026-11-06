"""Catalog access: OpenRouter alias resolution and Synthetic ``syn:*`` discovery.

Both catalogs are public (no auth), change on release timescales, and are read
on the request hot path, so results are cached twice:

* in-process, for the life of the Hermes process;
* on disk under ``$HERMES_HOME/cache/auto-router/``, so a fresh process (a new
  CLI invocation, a restarted gateway) does not pay a network round-trip before
  its first request.

A cold cache never blocks a request: callers get ``[]`` and the Auto Router runs
unconstrained for that one call, then the next call is constrained.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any

from .pool import alias_targets, resolve_pool

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
SYNTHETIC_MODELS_URL = "https://api.synthetic.new/openai/v1/models"
SYNTHETIC_ALIAS_PREFIX = "syn:"

DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 8.0

_MEMORY: dict[str, tuple[float, Any]] = {}


def _cache_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "cache" / "auto-router" / "catalog.json"


def _read_disk(key: str, ttl: float) -> Any:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        entry = data.get(key) or {}
        if time.time() - float(entry.get("at", 0)) < ttl:
            return entry.get("value")
    except Exception:
        pass
    return None


def _write_disk(key: str, value: Any) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        data[key] = {"at": time.time(), "value": value}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # cache is an optimisation, never a failure mode
        logger.debug("auto-router: catalog cache write failed: %s", exc)


def _cached(key: str, ttl: float, produce) -> Any:
    now = time.time()
    hit = _MEMORY.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    disk = _read_disk(key, ttl)
    if disk is not None:
        _MEMORY[key] = (now, disk)
        return disk
    value = produce()
    if value:
        _MEMORY[key] = (now, value)
        _write_disk(key, value)
    return value


def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "hermes-cli"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def openrouter_models(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    payload = fetch_json(OPENROUTER_MODELS_URL, timeout)
    data = payload.get("data") if isinstance(payload, dict) else payload
    return [m for m in (data or []) if isinstance(m, dict)]


def synthetic_models(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    payload = fetch_json(SYNTHETIC_MODELS_URL, timeout)
    data = payload.get("data") if isinstance(payload, dict) else payload
    return [m for m in (data or []) if isinstance(m, dict)]


def synthetic_aliases(
    timeout: float = DEFAULT_TIMEOUT_SECONDS, *, ttl: float = DEFAULT_TTL_SECONDS
) -> list[str]:
    """The ``syn:*`` ids Synthetic currently publishes, in catalog order."""

    def produce() -> list[str]:
        out: list[str] = []
        for model in synthetic_models(timeout):
            model_id = model.get("id")
            if (
                isinstance(model_id, str)
                and model_id.startswith(SYNTHETIC_ALIAS_PREFIX)
                and model_id not in out
            ):
                out.append(model_id)
        return out

    return _cached("synthetic-aliases", ttl, produce) or []


def resolved_pool(
    aliases: list[str] | tuple[str, ...],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ttl: float = DEFAULT_TTL_SECONDS,
    refresh: bool = False,
) -> list[str]:
    """Concrete slugs for *aliases*, cached per alias set."""
    key = "pool:" + hashlib.sha1(",".join(aliases).encode("utf-8")).hexdigest()[:16]
    if refresh:
        _MEMORY.pop(key, None)
    return _cached(key, ttl, lambda: resolve_pool(aliases, alias_targets(openrouter_models(timeout)))) or []


def warm(aliases: list[str] | tuple[str, ...]) -> None:
    """Populate the pool cache off the request path. Never raises."""
    try:
        resolved_pool(aliases)
    except Exception as exc:
        logger.debug("auto-router: pool warm failed: %s", exc)
