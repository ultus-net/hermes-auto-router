# Architecture

Two plugin halves, because Hermes discovers them through two different systems.
They install independently, so neither may depend on the other's import graph.
One more seam, the mail organiser, rides inside the general plugin.

## 0. Seams and their guards

A seam is one independently registerable part of the plugin. Each one registers
inside its own guard in the root `register(ctx)`, and the router registers first:
a session with no routing is broken and a session with no mail is not.

The root package stays import-light. Every seam imports lazily inside its own
guard, so a mail import error, a missing mail dependency, or a raising mail
`register()` logs with the plugin id and disables mail only. Hermes still
disposes every registration when `register()` itself raises, or when a load
overruns `plugins.load_timeout_seconds`. That is why the guard, not a retry, is
the routing protection: one plugin load, one deadline.

No seam imports another. A shared helper waits for a third real use in a
`personal_core` package.

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

## 4. `mail_organiser/` — the mail seam

The mail seam owns one SQLite database from
`plugin_db("hermes-personal")`, so mail state survives a plugin update or
removal and never lands in the plugin tree. The store name is deliberately the
post-rename name from D11, so the data survives that rename too.

Phase 1 ships the durable half with no user-facing surface: the eight-state
journal, the rule engine with one precedence column, the four-behaviour policy
gate, and an in-memory provider for the suite. Phase 2 adds the Gmail adapter and
the taxonomy bootstrap, still with no command or tool. `register(ctx)` applies
the schema manifest and attaches the store.

The Gmail adapter derives its allowlist from the protocol. Every `Mailbox`
operation names exactly one method and path, and `send`, `drafts`, and `delete`
raise before a URL is built, because `gmail.modify` technically reaches them.
The adapter asks Hermes for the grant on every call, so the token never enters
this code. The manifest declares the `google` entry with the `gmail.modify`
scope.

The invariants the rest of the seam depends on:

| Invariant | Enforced by |
| --- | --- |
| The cursor advances only with a journalled page | one transaction in `store.commit_page` |
| A decision is single-use and expires | the resolve predicate includes `expires_at` |
| One sweep per mailbox | a fenced lease with a takeover counter |
| Operator 40 > learned 30 > classifier 20 | one rank column, one ordering |
| A sensitive folder stays out of the classifier's reach | the gate, keyed on canonical D14 identity |
| Filing waits for an approved taxonomy | the gate, before any provider call |
| An expired cursor recovers in one cycle | `sweep._open_page` answers with one `resync` and keeps its cursor |
| A refused Gmail family never reaches the network | the route allowlist in `gmail.py` |

## Cache layers

| Layer | Lives for | Filled by |
| --- | --- | --- |
| in-process | the Hermes process | first lookup, disk hit, or background refresh |
| on disk (`$HERMES_HOME/cache/auto-router/`) | six hours | every successful fetch |
| negative entry | 60 seconds | any failed or empty lookup |

The pool itself is not cached. It is a pure function of the cached alias target
map, so every alias set shares one catalog cache.

Back to [README](../README.md).
