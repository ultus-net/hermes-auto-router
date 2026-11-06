"""Catalog access: OpenRouter alias resolution and Synthetic ``syn:*`` discovery.

Both catalogs are public (no auth), change on release timescales, and are read
on the request hot path, so results are cached twice:

* in-process, for the life of the Hermes process;
* on disk under ``$HERMES_HOME/cache/auto-router/``, so a fresh process (a new
  CLI invocation, a restarted gateway) does not pay a network round-trip before
  its first request.

Cached values are the two catalogs themselves: the Synthetic alias list and the
OpenRouter alias target map. The Auto Router pool is a pure function of the
target map, so it is recomputed per call instead of cached a third time.

The request path never fetches. On a cold or expired cache it serves the stale
in-process value if one exists, otherwise ``[]``, and schedules a daemon
refresh. The Auto Router runs unconstrained for that one call, and the next call
is constrained.

Empty results are cached too, for ``NEGATIVE_TTL_SECONDS``. Without that, a
catalog outage or an empty pool would turn every ``openrouter/auto`` request
into another eight-second fetch.
"""

from __future__ import annotations

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
SYNTHETIC_ALIASES_KEY = "synthetic-aliases"

DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 8.0
NEGATIVE_TTL_SECONDS = 60.0

_MEMORY: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_REFRESHING: set[str] = set()
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


def _lookup(key: str, ttl: float) -> Any:
    """Fresh value from memory or disk, or ``_MISSING``.

    A disk hit also fills the in-process layer. Expired memory entries stay put,
    so the request path can still serve them as stale.

    ``_load`` calls this under ``_LOCK``. The request path calls it unlocked on
    purpose: holding ``_LOCK`` across the disk read would block other callers.
    Dict get and set are atomic, so the unlocked probe cannot corrupt ``_MEMORY``.
    """
    hit = _MEMORY.get(key)
    if hit and _fresh(hit[0], hit[1], ttl):
        return hit[1]
    disk = _read_disk(key, ttl)
    if disk is not _MISSING:
        _MEMORY[key] = (time.time(), disk)
        return disk
    return _MISSING


def _load(key: str, ttl: float, produce: Callable[[], Any], *, refresh: bool) -> Any:
    """Return the cached value or produce a new one. The caller holds ``_LOCK``."""
    if not refresh:
        hit = _lookup(key, ttl)
        if hit is not _MISSING:
            return hit
    try:
        value = produce()
    except Exception:
        # Record the failure before re-raising so the next caller does not
        # repeat the network call inside the negative TTL window.
        _MEMORY[key] = (time.time(), [])
        _write_disk(key, [])
        raise
    _MEMORY[key] = (time.time(), value)
    _write_disk(key, value)
    return value


def _refresh(key: str, ttl: float, produce: Callable[[], Any]) -> None:
    try:
        with _LOCK:
            _load(key, ttl, produce, refresh=False)
    except Exception as exc:
        # One log per failed refresh (the negative cache rate-limits retries).
        logger.warning("auto-router: background catalog refresh failed: %s", exc)
    finally:
        with _REFRESH_LOCK:
            _REFRESHING.discard(key)


def _refresh_in_background(key: str, ttl: float, produce: Callable[[], Any]) -> None:
    """Refresh *key* off the request path, at most once at a time."""
    with _REFRESH_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)
    try:
        threading.Thread(
            target=_refresh,
            args=(key, ttl, produce),
            name="auto-router-catalog-refresh",
            daemon=True,
        ).start()
    except Exception as exc:
        logger.warning("auto-router: could not start catalog refresh: %s", exc)
        with _REFRESH_LOCK:
            _REFRESHING.discard(key)


def _cached(
    key: str,
    ttl: float,
    produce: Callable[[], Any],
    *,
    blocking: bool = True,
    refresh: bool = False,
) -> Any:
    """Return the cached value for *key*, calling *produce* on a miss.

    ``blocking=False`` never calls *produce*. It returns the stale in-process
    value when one exists, otherwise ``_MISSING``, and schedules a daemon
    refresh. Callers on the request path use this mode.

    ``refresh=True`` skips both cache layers and overwrites them. It is a
    blocking-only mode: a non-blocking call still just schedules a refresh.
    """
    if not refresh:
        hit = _lookup(key, ttl)
        if hit is not _MISSING:
            return hit
    if not blocking:
        stale = _MEMORY.get(key)
        _refresh_in_background(key, ttl, produce)
        return stale[1] if stale else _MISSING
    with _LOCK:
        return _load(key, ttl, produce, refresh=refresh)


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
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    ttl: float = DEFAULT_TTL_SECONDS,
    refresh: bool = False,
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

    aliases = _cached(SYNTHETIC_ALIASES_KEY, ttl, produce, refresh=refresh)
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
    """Concrete slugs for *aliases*.

    The pool is a pure function of the cached target map, so it is not cached
    separately. With ``blocking=False`` the map may be stale or unavailable;
    either way this returns without fetching.
    """
    targets = alias_target_map(timeout=timeout, ttl=ttl, refresh=refresh, blocking=blocking)
    return resolve_pool(tuple(aliases or ()), targets)


def warm() -> None:
    """Populate the target-map cache off the request path. Never raises."""
    try:
        alias_target_map()
    except Exception as exc:
        logger.debug("auto-router: catalog warm failed: %s", exc)
