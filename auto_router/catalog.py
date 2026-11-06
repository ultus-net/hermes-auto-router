"""Catalog access: OpenRouter alias resolution and Synthetic ``syn:*`` discovery.

Both catalogs are public (no auth), change on release timescales, and are read
on the request hot path, so results are cached twice:

* in-process, for the life of the Hermes process;
* on disk under ``$HERMES_HOME/cache/auto-router/``, so a fresh process (a new
  CLI invocation, a restarted gateway) does not pay a network round-trip before
  its first request.

A cold cache never blocks a request: the request path takes the cache lock
without waiting, so it returns ``[]`` while another thread fetches. The Auto
Router then runs unconstrained for that one call, and the next call is
constrained.

Empty results are cached too, for ``NEGATIVE_TTL_SECONDS``. Without that, a
catalog outage or an empty pool would turn every ``openrouter/auto`` request
into another eight-second fetch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .pool import alias_targets, resolve_pool

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
SYNTHETIC_MODELS_URL = "https://api.synthetic.new/openai/v1/models"
SYNTHETIC_ALIAS_PREFIX = "syn:"
ALIAS_TARGETS_KEY = "openrouter-alias-targets"

DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 8.0
NEGATIVE_TTL_SECONDS = 60.0

_MEMORY: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()
_MISSING = object()


def _cache_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "cache" / "auto-router" / "catalog.json"


def _fresh(at: float, value: Any, ttl: float) -> bool:
    """True while *value* is younger than its lifetime.

    An empty value is a recorded failure, so it expires on the short negative
    TTL instead of hiding a recovered catalog for six hours.
    """
    return time.time() - at < (ttl if value else NEGATIVE_TTL_SECONDS)


def _read_disk(key: str, ttl: float) -> Any:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _MISSING
    except Exception as exc:
        logger.debug("auto-router: catalog cache read failed: %s", exc)
        return _MISSING
    entry = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return _MISSING
    try:
        at = float(entry.get("at", 0))
    except (TypeError, ValueError):
        return _MISSING
    value = entry.get("value")
    if value is None or not _fresh(at, value, ttl):
        return _MISSING
    return value


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


def _cached(
    key: str,
    ttl: float,
    produce: Callable[[], Any],
    *,
    blocking: bool = True,
    refresh: bool = False,
) -> Any:
    """Return the cached value for *key*, calling *produce* on a miss.

    ``blocking=False`` never waits for another thread's fetch; it returns
    ``_MISSING`` instead so the caller can degrade to an unconstrained request.
    ``refresh=True`` skips both cache layers and overwrites them.

    A failed *produce* records an empty value before re-raising, so the next
    caller does not repeat the network call inside the negative TTL window.
    """
    if not refresh:
        hit = _MEMORY.get(key)
        if hit and _fresh(hit[0], hit[1], ttl):
            return hit[1]
        disk = _read_disk(key, ttl)
        if disk is not _MISSING:
            _MEMORY[key] = (time.time(), disk)
            return disk

    if not _LOCK.acquire(blocking=blocking):
        hit = _MEMORY.get(key)
        return hit[1] if hit else _MISSING
    try:
        if not refresh:
            hit = _MEMORY.get(key)
            if hit and _fresh(hit[0], hit[1], ttl):
                return hit[1]
            disk = _read_disk(key, ttl)
            if disk is not _MISSING:
                _MEMORY[key] = (time.time(), disk)
                return disk
        try:
            value = produce()
        except Exception:
            _MEMORY[key] = (time.time(), [])
            _write_disk(key, [])
            raise
        _MEMORY[key] = (time.time(), value)
        _write_disk(key, value)
        return value
    finally:
        _LOCK.release()


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

    aliases = _cached("synthetic-aliases", ttl, produce)
    return aliases if isinstance(aliases, list) else []


def alias_target_map(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ttl: float = DEFAULT_TTL_SECONDS,
    refresh: bool = False,
    blocking: bool = True,
) -> dict[str, str]:
    """Cached ``~...-latest`` alias to concrete slug mapping."""
    targets = _cached(
        ALIAS_TARGETS_KEY,
        ttl,
        lambda: alias_targets(openrouter_models(timeout)),
        blocking=blocking,
        refresh=refresh,
    )
    return targets if isinstance(targets, dict) else {}


def resolved_pool(
    aliases: list[str] | tuple[str, ...],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ttl: float = DEFAULT_TTL_SECONDS,
    refresh: bool = False,
    blocking: bool = True,
) -> list[str]:
    """Concrete slugs for *aliases*, cached per alias set.

    The target map is resolved before the pool cache lock is taken. Resolving it
    inside the pool's ``produce`` callback would re-enter the same lock and
    deadlock.
    """
    aliases = tuple(aliases or ())
    targets = alias_target_map(timeout=timeout, ttl=ttl, refresh=refresh, blocking=blocking)
    if not targets and not blocking:
        # Another thread holds the cache lock and is fetching the catalog. Do
        # not cache this empty pool: the fetch in flight will fill the cache.
        return []
    digest = hashlib.sha256(",".join(aliases).encode("utf-8")).hexdigest()[:16]
    pool = _cached(
        "pool:" + digest,
        ttl,
        lambda: resolve_pool(aliases, targets),
        blocking=blocking,
        refresh=refresh,
    )
    return pool if isinstance(pool, list) else []


def warm(aliases: list[str] | tuple[str, ...]) -> None:
    """Populate the pool cache off the request path. Never raises."""
    try:
        resolved_pool(aliases)
    except Exception as exc:
        logger.debug("auto-router: pool warm failed: %s", exc)
