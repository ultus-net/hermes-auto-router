# Architecture

Two plugin halves, because Hermes discovers them through two different systems.
They install independently, so neither may depend on the other's import graph.

## 1. `providers/synthetic/` — the Synthetic provider

Synthetic's `syn:*` aliases are absent from the public models.dev catalog, so
Hermes cannot select them out of the box. This model-provider plugin discovers
them from Synthetic's OpenAI-compatible `/models` endpoint and registers them
with context limits, vision capability, and reasoning support.

Only `syn:*` ids are kept. Concrete `hf:*` / `infinite:*` ids are ignored: the
point is to never pin a version.

## 2. `auto_router/` — the Auto Router constraint

The Auto Router accepts an `allowed_models` constraint, **but it only matches
concrete catalog ids.** The `~…-latest` aliases resolve fine as a top-level
`model`, but inside `allowed_models` they match nothing. The candidate pool
collapses to zero and every request fails:

```
404  No models match your request and model restrictions
```

An `llm_request` middleware resolves each alias to its current concrete slug
(`alias_target.slug`) and injects that list into every `openrouter/auto`
request. The alias target map is cached in-process and on disk for six hours,
and warmed in a background thread at plugin load. The request path never
fetches: on a cold or expired cache it uses the stale in-process map when one
exists, otherwise it leaves the router unconstrained for that call. A fresh
process starts empty and fills the map in a background thread. An empty or
failed lookup is cached for 60 seconds, so an outage does not turn every
request into another eight-second fetch.

## 3. Failover is Hermes' own

The OpenCode plugin had to observe failures and re-send the turn, because
OpenCode exposes no cross-provider failover. Hermes does: `fallback_providers`
switches provider mid-turn on 429/5xx/auth errors, preserves the conversation,
re-resolves reasoning effort, and restores the primary on the next turn.

So `hermes auto-router apply` writes the chain instead of re-implementing it.
Re-implementing would fight the runtime's retry, cooldown, and per-turn
restoration logic.

## Cache layers

| Layer | Lives for | Filled by |
| --- | --- | --- |
| in-process | the Hermes process | first lookup, disk hit, or background refresh |
| on disk (`$HERMES_HOME/cache/auto-router/`) | six hours | every successful fetch |
| negative entry | 60 seconds | any failed or empty lookup |

The pool itself is not cached. It is a pure function of the cached alias target
map, so every alias set shares one catalog cache.

Back to [README](../README.md).
