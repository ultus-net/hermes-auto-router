"""SQLite schema and the journalled state machine for the mail seam.

``commit_page`` journals a decided page and advances the listing cursor in one
transaction. A failure rolls both back, so an unjournalled message is re-listed
rather than lost. Identity is ``(mailbox, stable_id)``; the content hash only
says whether a decision is still current.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import (
    D14_SENSITIVE_NAMES,
    FINAL_STATES,
    OPEN_STATES,
    Behaviour,
    Decision,
    JournalEntry,
    JournalState,
    MailboxPolicy,
    PolicyAction,
    Risk,
    Rule,
)

SCHEMA_VERSION = 1

#: The ordered manifest the migrator applies and a restore compares against.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS mail_mailboxes (
      account TEXT PRIMARY KEY,
      provider TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1,
      paused INTEGER NOT NULL DEFAULT 0,
      dry_run INTEGER NOT NULL DEFAULT 1,
      policy_json TEXT NOT NULL,
      policy_version INTEGER NOT NULL DEFAULT 0,
      taxonomy_version INTEGER NOT NULL DEFAULT 0,
      ruleset_generation INTEGER NOT NULL DEFAULT 0,
      validator_version INTEGER NOT NULL DEFAULT 1,
      cursor TEXT,
      cursor_issued_at TEXT,
      backfill_cursor TEXT,
      backfill_state TEXT NOT NULL DEFAULT 'idle',
      updated_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_messages (
      mailbox TEXT NOT NULL,
      message_id TEXT NOT NULL,
      stable_id TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      state TEXT NOT NULL,
      decision_json TEXT,
      decided_by TEXT,
      policy_version INTEGER NOT NULL,
      taxonomy_version INTEGER NOT NULL,
      rule_id TEXT,
      rule_version INTEGER,
      validator_version INTEGER NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      retry_after TEXT,
      decided_at TEXT,
      applied_at TEXT,
      PRIMARY KEY (mailbox, message_id))
    """,
    "CREATE INDEX IF NOT EXISTS mail_messages_stable ON mail_messages (mailbox, stable_id)",
    """
    CREATE INDEX IF NOT EXISTS mail_messages_open ON mail_messages (mailbox, state)
      WHERE state IN ('pending', 'inflight', 'retryable', 'needs_decision', 'dry_run')
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_message_ids (
      mailbox TEXT NOT NULL,
      provider_id TEXT NOT NULL,
      current_id TEXT NOT NULL,
      PRIMARY KEY (mailbox, provider_id))
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_actions (
      id TEXT PRIMARY KEY,
      mailbox TEXT NOT NULL,
      message_id TEXT NOT NULL,
      stable_id TEXT NOT NULL,
      action_key TEXT NOT NULL,
      sweep_id TEXT NOT NULL,
      verb TEXT NOT NULL,
      target TEXT,
      state TEXT NOT NULL,
      pre_ref TEXT,
      post_ref TEXT,
      error TEXT,
      dispatched_at TEXT,
      applied_at TEXT,
      UNIQUE (action_key))
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_decisions (
      id TEXT PRIMARY KEY,
      mailbox TEXT NOT NULL,
      message_id TEXT NOT NULL,
      stable_id TEXT NOT NULL,
      decision_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      lease_holder TEXT,
      lease_until TEXT,
      fencing INTEGER NOT NULL DEFAULT 0,
      resolved_at TEXT,
      resolution TEXT)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS mail_decisions_open ON mail_decisions (mailbox, stable_id)
      WHERE resolved_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_sweeps (
      id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      scheduled_for TEXT NOT NULL,
      execution_id TEXT,
      mailbox TEXT NOT NULL,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      status TEXT NOT NULL,
      counters_json TEXT,
      UNIQUE (job_id, scheduled_for, mailbox))
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_schedule (
      job_id TEXT PRIMARY KEY,
      mailbox TEXT NOT NULL,
      interval_minutes INTEGER NOT NULL,
      grace_minutes INTEGER NOT NULL DEFAULT 30,
      anchor_at TEXT NOT NULL,
      last_expected_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_leases (
      mailbox TEXT PRIMARY KEY,
      holder TEXT NOT NULL,
      fencing INTEGER NOT NULL DEFAULT 0,
      expires_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_rules (
      id TEXT PRIMARY KEY,
      mailbox TEXT NOT NULL,
      matcher_json TEXT NOT NULL,
      target TEXT NOT NULL,
      rank INTEGER NOT NULL,
      priority INTEGER NOT NULL,
      source TEXT NOT NULL,
      version INTEGER NOT NULL DEFAULT 1,
      enabled INTEGER NOT NULL DEFAULT 1)
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_rule_suggestions (
      mailbox TEXT NOT NULL,
      key TEXT NOT NULL,
      target TEXT NOT NULL,
      evidence_json TEXT NOT NULL,
      dismissed_at TEXT,
      PRIMARY KEY (mailbox, key))
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_rule_deletions (
      receipt TEXT PRIMARY KEY,
      mailbox TEXT NOT NULL,
      rule_json TEXT NOT NULL,
      deleted_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_taxonomy (
      mailbox TEXT NOT NULL,
      version INTEGER NOT NULL,
      state TEXT NOT NULL,
      json TEXT NOT NULL,
      PRIMARY KEY (mailbox, version))
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_sensitive_keys (
      mailbox TEXT NOT NULL,
      key TEXT NOT NULL,
      display_name TEXT NOT NULL,
      source TEXT NOT NULL,
      marked_at TEXT NOT NULL,
      PRIMARY KEY (mailbox, key))
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_backfill_plans (
      id TEXT PRIMARY KEY,
      mailbox TEXT NOT NULL,
      taxonomy_version INTEGER NOT NULL,
      ruleset_generation INTEGER NOT NULL,
      supersedes TEXT,
      created_at TEXT NOT NULL,
      approved_at TEXT,
      digest TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_backfill_items (
      plan_id TEXT NOT NULL,
      message_id TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      target TEXT NOT NULL,
      rule_id TEXT,
      rule_version INTEGER,
      state TEXT NOT NULL DEFAULT 'pending',
      superseded_by TEXT,
      PRIMARY KEY (plan_id, message_id))
    """,
    """
    CREATE TABLE IF NOT EXISTS mail_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL)
    """,
)

#: States a page may journal as its first state. ``inflight`` and ``confirmed``
#: only exist after a provider call, never as an initial decision.
INITIAL_STATES: frozenset[JournalState] = frozenset(
    {
        JournalState.PENDING,
        JournalState.DRY_RUN,
        JournalState.NEEDS_DECISION,
        JournalState.BLOCKED,
        JournalState.INVALID,
    }
)

ALLOWED_TRANSITIONS: Mapping[JournalState, frozenset[JournalState]] = {
    JournalState.PENDING: frozenset(
        {
            JournalState.INFLIGHT,
            JournalState.DRY_RUN,
            JournalState.CONFIRMED,
            JournalState.RETRYABLE,
            JournalState.NEEDS_DECISION,
            JournalState.BLOCKED,
            JournalState.INVALID,
        }
    ),
    JournalState.INFLIGHT: frozenset(
        {
            JournalState.CONFIRMED,
            JournalState.RETRYABLE,
            JournalState.NEEDS_DECISION,
            JournalState.BLOCKED,
        }
    ),
    JournalState.RETRYABLE: frozenset(
        {
            JournalState.PENDING,
            JournalState.INFLIGHT,
            JournalState.NEEDS_DECISION,
            JournalState.BLOCKED,
        }
    ),
    JournalState.DRY_RUN: frozenset(
        {
            JournalState.PENDING,
            JournalState.INFLIGHT,
            JournalState.CONFIRMED,
            JournalState.NEEDS_DECISION,
            JournalState.BLOCKED,
            JournalState.INVALID,
        }
    ),
    JournalState.NEEDS_DECISION: frozenset(
        {
            JournalState.PENDING,
            JournalState.INFLIGHT,
            JournalState.CONFIRMED,
            JournalState.BLOCKED,
            JournalState.INVALID,
        }
    ),
    JournalState.INVALID: frozenset(
        {
            JournalState.PENDING,
            JournalState.INFLIGHT,
            JournalState.NEEDS_DECISION,
            JournalState.BLOCKED,
        }
    ),
    JournalState.CONFIRMED: frozenset(),
    JournalState.BLOCKED: frozenset(),
}


class StoreError(Exception):
    """Base class for a refusal the caller must handle."""


class SweepBusy(StoreError):
    """Another holder owns the mailbox lease."""


class InvalidTransition(StoreError):
    """A journal state change the state machine does not allow."""


class MessageNotFound(StoreError):
    """No journal row for that mailbox and message id."""


class MailboxMismatch(StoreError):
    """A page from one mailbox was offered to another mailbox's journal."""


@dataclass(frozen=True)
class MailboxRow:
    """One mailbox's durable settings and frontier."""

    account: str
    provider: str
    enabled: bool
    paused: bool
    dry_run: bool
    policy: MailboxPolicy
    policy_version: int
    taxonomy_version: int
    ruleset_generation: int
    validator_version: int
    cursor: str | None
    cursor_issued_at: str | None


@dataclass(frozen=True)
class TaxonomyFolder:
    """One approved folder and its classifier reach."""

    name: str
    risk: Risk
    description: str = ""


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Apply the manifest and record its version. Idempotent by design."""

    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO mail_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def schema_manifest(conn: sqlite3.Connection) -> list[str]:
    """The live tables and indexes, for the restore comparison."""

    rows = conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    return [f"{row['type']}:{row['name']}" for row in rows]


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM mail_meta WHERE key='schema_version'").fetchone()
    return int(row["value"]) if row else 0


def encode_policy(behaviours: Mapping[PolicyAction, Behaviour]) -> str:
    return json.dumps({action.value: behaviour.value for action, behaviour in behaviours.items()}, sort_keys=True)


def decode_policy(raw: str, version: int = 0) -> MailboxPolicy:
    data = json.loads(raw)
    behaviours: dict[PolicyAction, Behaviour] = {}
    for key, value in data.items():
        try:
            behaviours[PolicyAction(key)] = Behaviour(value)
        except ValueError:
            continue
    return MailboxPolicy(behaviours=behaviours, version=version)


def ensure_mailbox(
    conn: sqlite3.Connection,
    account: str,
    provider: str,
    *,
    behaviours: Mapping[PolicyAction, Behaviour],
    dry_run: bool = True,
    now: datetime | None = None,
) -> None:
    stamp = iso(now or now_utc())
    with conn:
        conn.execute(
            "INSERT INTO mail_mailboxes "
            "(account, provider, enabled, paused, dry_run, policy_json, policy_version, "
            "taxonomy_version, ruleset_generation, validator_version, updated_at) "
            "VALUES (?, ?, 1, 0, ?, ?, 0, 0, 0, 1, ?) "
            "ON CONFLICT(account) DO NOTHING",
            (account, provider, int(dry_run), encode_policy(behaviours), stamp),
        )


def mailbox(conn: sqlite3.Connection, account: str) -> MailboxRow:
    row = conn.execute("SELECT * FROM mail_mailboxes WHERE account=?", (account,)).fetchone()
    if row is None:
        raise MessageNotFound(f"unknown mailbox {account}")
    return MailboxRow(
        account=row["account"],
        provider=row["provider"],
        enabled=bool(row["enabled"]),
        paused=bool(row["paused"]),
        dry_run=bool(row["dry_run"]),
        policy=decode_policy(row["policy_json"], row["policy_version"]),
        policy_version=row["policy_version"],
        taxonomy_version=row["taxonomy_version"],
        ruleset_generation=row["ruleset_generation"],
        validator_version=row["validator_version"],
        cursor=row["cursor"],
        cursor_issued_at=row["cursor_issued_at"],
    )


def set_paused(conn: sqlite3.Connection, account: str, paused: bool, *, now: datetime | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE mail_mailboxes SET paused=?, updated_at=? WHERE account=?",
            (int(paused), iso(now or now_utc()), account),
        )


def set_dry_run(conn: sqlite3.Connection, account: str, dry_run: bool, *, now: datetime | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE mail_mailboxes SET dry_run=?, updated_at=? WHERE account=?",
            (int(dry_run), iso(now or now_utc()), account),
        )


def commit_page(
    conn: sqlite3.Connection,
    account: str,
    entries: Iterable[JournalEntry],
    *,
    cursor: str | None = None,
    issued_at: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """Journal a decided page and advance the cursor atomically.

    The cursor moves only when every entry was written. A caller that holds a
    page but fails to journal it leaves the frontier behind, which re-lists the
    messages. The return value is the number of new rows written.
    """

    written = 0
    stamp = iso(now or now_utc())
    planned = list(entries)
    for entry in planned:
        if entry.ref.mailbox != account:
            raise MailboxMismatch(f"{entry.ref.mailbox} page committed to {account}")
    with conn:
        for entry in planned:
            written += _write_entry(conn, entry, stamp)
        if cursor is not None:
            conn.execute(
                "UPDATE mail_mailboxes SET cursor=?, cursor_issued_at=?, updated_at=? WHERE account=?",
                (cursor, iso(issued_at) if issued_at else stamp, stamp, account),
            )
    return written


def _write_entry(conn: sqlite3.Connection, entry: JournalEntry, stamp: str) -> int:
    if entry.state not in INITIAL_STATES:
        raise InvalidTransition(f"{entry.state.value} is not an initial journal state")

    existing = conn.execute(
        "SELECT message_id, stable_id, state FROM mail_messages WHERE mailbox=? AND message_id=?",
        (entry.ref.mailbox, entry.ref.message_id),
    ).fetchone()

    if (
        existing is not None
        and existing["stable_id"] == entry.ref.stable_id
        and JournalState(existing["state"]) in OPEN_STATES | FINAL_STATES
    ):
        # A re-listed message keeps its first decision. An open row is resumed
        # through reconcile; a final row is already done.
        return 0

    open_row = conn.execute(
        "SELECT message_id FROM mail_messages WHERE mailbox=? AND stable_id=? AND state IN "
        "('pending', 'inflight', 'retryable', 'needs_decision', 'dry_run')",
        (entry.ref.mailbox, entry.ref.stable_id),
    ).fetchone()
    if open_row is not None and open_row["message_id"] != entry.ref.message_id:
        # Same identity under a new provider id: reconcile owns it.
        return 0

    conn.execute(
        "INSERT INTO mail_messages "
        "(mailbox, message_id, stable_id, content_hash, state, decision_json, decided_by, "
        "policy_version, taxonomy_version, rule_id, rule_version, validator_version, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(mailbox, message_id) DO UPDATE SET "
        "stable_id=excluded.stable_id, content_hash=excluded.content_hash, state=excluded.state, "
        "decision_json=excluded.decision_json, decided_by=excluded.decided_by, "
        "policy_version=excluded.policy_version, taxonomy_version=excluded.taxonomy_version, "
        "rule_id=excluded.rule_id, rule_version=excluded.rule_version, "
        "validator_version=excluded.validator_version, decided_at=excluded.decided_at",
        (
            entry.ref.mailbox,
            entry.ref.message_id,
            entry.ref.stable_id,
            entry.content_hash,
            entry.state.value,
            entry.decision.to_json(),
            entry.decided_by,
            entry.policy_version,
            entry.taxonomy_version,
            entry.rule_id,
            entry.rule_version,
            entry.validator_version,
            stamp,
        ),
    )
    return 1


def message_row(conn: sqlite3.Connection, account: str, message_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM mail_messages WHERE mailbox=? AND message_id=?",
        (account, message_id),
    ).fetchone()
    if row is None:
        raise MessageNotFound(f"{account}/{message_id}")
    return row


def record_message_id(conn: sqlite3.Connection, account: str, provider_id: str, current_id: str) -> None:
    """Remember the provider id a move retired, so an old reference still resolves."""

    with conn:
        conn.execute(
            "INSERT INTO mail_message_ids(mailbox, provider_id, current_id) VALUES (?, ?, ?) "
            "ON CONFLICT(mailbox, provider_id) DO UPDATE SET current_id=excluded.current_id",
            (account, provider_id, current_id),
        )


def set_state(
    conn: sqlite3.Connection,
    account: str,
    message_id: str,
    state: JournalState,
    *,
    expected: JournalState | None = None,
    decision: Decision | None = None,
    attempts: int | None = None,
    retry_after: str | None = None,
    applied_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Move one journal row, refusing a transition the machine forbids."""

    current = message_row(conn, account, message_id)
    current_state = JournalState(current["state"])
    if expected is not None and current_state is not expected:
        return False
    if state not in ALLOWED_TRANSITIONS[current_state]:
        raise InvalidTransition(f"{current_state.value} -> {state.value}")

    assignments = ["state=?"]
    params: list[Any] = [state.value]
    if decision is not None:
        assignments.append("decision_json=?")
        params.append(decision.to_json())
    if attempts is not None:
        assignments.append("attempts=?")
        params.append(attempts)
    if retry_after is not None:
        assignments.append("retry_after=?")
        params.append(retry_after)
    if applied_at is not None:
        assignments.append("applied_at=?")
        params.append(iso(applied_at))
    if now is not None:
        assignments.append("decided_at=?")
        params.append(iso(now))
    params.extend([account, message_id, current_state.value])

    with conn:
        result = conn.execute(
            f"UPDATE mail_messages SET {', '.join(assignments)} "
            "WHERE mailbox=? AND message_id=? AND state=?",
            tuple(params),
        )
    return result.rowcount == 1


def open_messages(conn: sqlite3.Connection, account: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM mail_messages WHERE mailbox=? AND state IN "
        "('pending', 'inflight', 'retryable', 'needs_decision', 'dry_run') "
        "ORDER BY decided_at, message_id",
        (account,),
    ).fetchall()


def take_lease(
    conn: sqlite3.Connection,
    account: str,
    holder: str,
    *,
    ttl_seconds: int = 300,
    now: datetime | None = None,
) -> int:
    """Take or renew the mailbox lease. Returns the fencing token."""

    moment = now or now_utc()
    stamp = iso(moment)
    expires = iso(moment + timedelta(seconds=ttl_seconds))
    with conn:
        row = conn.execute("SELECT holder, fencing, expires_at FROM mail_leases WHERE mailbox=?", (account,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO mail_leases(mailbox, holder, fencing, expires_at) VALUES (?, ?, 1, ?)",
                (account, holder, expires),
            )
            return 1
        if row["expires_at"] > stamp:
            raise SweepBusy(f"{account} is held by {row['holder']}")
        fence = int(row["fencing"]) + 1
        conn.execute(
            "UPDATE mail_leases SET holder=?, fencing=?, expires_at=? WHERE mailbox=?",
            (holder, fence, expires, account),
        )
        return fence


def release_lease(conn: sqlite3.Connection, account: str, holder: str, fencing: int) -> bool:
    with conn:
        result = conn.execute(
            "DELETE FROM mail_leases WHERE mailbox=? AND holder=? AND fencing=?",
            (account, holder, fencing),
        )
    return result.rowcount == 1


def start_sweep(
    conn: sqlite3.Connection,
    *,
    sweep_id: str,
    job_id: str,
    scheduled_for: str,
    mailbox: str,
    execution_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Open one sweep row. A repeat of the same occurrence is refused."""

    try:
        with conn:
            conn.execute(
                "INSERT INTO mail_sweeps "
                "(id, job_id, scheduled_for, execution_id, mailbox, started_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running')",
                (sweep_id, job_id, scheduled_for, execution_id, mailbox, iso(now or now_utc())),
            )
    except sqlite3.IntegrityError as exc:
        raise SweepBusy(f"{job_id} at {scheduled_for} already ran for {mailbox}") from exc


def finish_sweep(
    conn: sqlite3.Connection,
    sweep_id: str,
    *,
    status: str,
    counters: Mapping[str, Any],
    now: datetime | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE mail_sweeps SET finished_at=?, status=?, counters_json=? WHERE id=?",
            (iso(now or now_utc()), status, json.dumps(dict(counters), sort_keys=True), sweep_id),
        )


def open_decision(
    conn: sqlite3.Connection,
    *,
    account: str,
    message_id: str,
    stable_id: str,
    decision: Decision,
    expires_at: datetime,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Create the single open ask for a stable id, or return the existing one."""

    existing = conn.execute(
        "SELECT id FROM mail_decisions WHERE mailbox=? AND stable_id=? AND resolved_at IS NULL",
        (account, stable_id),
    ).fetchone()
    if existing is not None:
        return existing["id"], False

    decision_id = str(uuid.uuid4())
    with conn:
        conn.execute(
            "INSERT INTO mail_decisions "
            "(id, mailbox, message_id, stable_id, decision_json, created_at, expires_at, fencing) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                decision_id,
                account,
                message_id,
                stable_id,
                decision.to_json(),
                iso(now or now_utc()),
                iso(expires_at),
            ),
        )
    return decision_id, True


def claim_decision(
    conn: sqlite3.Connection,
    decision_id: str,
    holder: str,
    *,
    ttl_seconds: int = 300,
    now: datetime | None = None,
) -> int | None:
    """Lease an open, unexpired decision. Returns the fencing token, or None."""

    moment = now or now_utc()
    stamp = iso(moment)
    expires = iso(moment + timedelta(seconds=ttl_seconds))
    with conn:
        row = conn.execute("SELECT fencing FROM mail_decisions WHERE id=?", (decision_id,)).fetchone()
        if row is None:
            return None
        fence = int(row["fencing"]) + 1
        result = conn.execute(
            "UPDATE mail_decisions SET lease_holder=?, lease_until=?, fencing=? "
            "WHERE id=? AND resolved_at IS NULL AND expires_at > ? "
            "AND (lease_until IS NULL OR lease_until < ?)",
            (holder, expires, fence, decision_id, stamp, stamp),
        )
    return fence if result.rowcount == 1 else None


def resolve_decision(
    conn: sqlite3.Connection,
    decision_id: str,
    resolution: str,
    *,
    holder: str | None = None,
    fencing: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Resolve one decision atomically, with expiry and lease in the predicate."""

    stamp = iso(now or now_utc())
    sql = (
        "UPDATE mail_decisions SET resolved_at=?, resolution=? "
        "WHERE id=? AND resolved_at IS NULL AND expires_at > ? "
        "AND (lease_until IS NULL OR lease_until < ?)"
    )
    params: list[Any] = [stamp, resolution, decision_id, stamp, stamp]
    if holder is not None:
        sql += " AND lease_holder=?"
        params.append(holder)
    if fencing is not None:
        sql += " AND fencing=?"
        params.append(fencing)
    with conn:
        result = conn.execute(sql, tuple(params))
    return result.rowcount == 1


def insert_rule(
    conn: sqlite3.Connection,
    *,
    rule_id: str,
    account: str,
    matcher: Mapping[str, Any],
    target: str,
    source: str,
    rank: int,
    priority: int,
    version: int = 1,
    enabled: bool = True,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO mail_rules "
            "(id, mailbox, matcher_json, target, rank, priority, source, version, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET matcher_json=excluded.matcher_json, target=excluded.target, "
            "rank=excluded.rank, priority=excluded.priority, source=excluded.source, "
            "version=excluded.version, enabled=excluded.enabled",
            (
                rule_id,
                account,
                json.dumps(dict(matcher), sort_keys=True),
                target,
                rank,
                priority,
                source,
                version,
                int(enabled),
            ),
        )
        bump_ruleset_generation(conn, account)


def bump_ruleset_generation(
    conn: sqlite3.Connection,
    account: str,
    *,
    now: datetime | None = None,
) -> int:
    """Advance the ruleset generation. The caller owns the transaction."""

    conn.execute(
        "UPDATE mail_mailboxes SET ruleset_generation=ruleset_generation+1, updated_at=? WHERE account=?",
        (iso(now or now_utc()), account),
    )
    row = conn.execute("SELECT ruleset_generation FROM mail_mailboxes WHERE account=?", (account,)).fetchone()
    return int(row["ruleset_generation"]) if row else 0


def enabled_rules(conn: sqlite3.Connection, account: str) -> list[Rule]:
    """Rules in precedence order: operator over learned over classifier."""

    rows = conn.execute(
        "SELECT * FROM mail_rules WHERE mailbox=? AND enabled=1 ORDER BY rank DESC, priority ASC, id ASC",
        (account,),
    ).fetchall()
    return [
        Rule(
            id=row["id"],
            mailbox=row["mailbox"],
            matcher=json.loads(row["matcher_json"]),
            target=row["target"],
            rank=row["rank"],
            priority=row["priority"],
            source=row["source"],
            version=row["version"],
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


def sensitive_key(name: str) -> str:
    """Canonical identity for a folder name, independent of display form."""

    folded = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(folded.split())


def mark_sensitive(
    conn: sqlite3.Connection,
    account: str,
    display_name: str,
    *,
    source: str = "operator",
    now: datetime | None = None,
) -> str:
    key = sensitive_key(display_name)
    with conn:
        conn.execute(
            "INSERT INTO mail_sensitive_keys(mailbox, key, display_name, source, marked_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(mailbox, key) DO NOTHING",
            (account, key, display_name, source, iso(now or now_utc())),
        )
    return key


def seed_sensitive(conn: sqlite3.Connection, account: str, *, now: datetime | None = None) -> None:
    for name in D14_SENSITIVE_NAMES:
        mark_sensitive(conn, account, name, source="d14", now=now)


def is_sensitive(conn: sqlite3.Connection, account: str, folder_name: str) -> bool:
    """Fail closed: a case, width, rename, or nested variant still matches.

    D14 names are always sensitive, even before a mailbox seeds its keys. The
    guarantee must not depend on a prior write.
    """

    key = sensitive_key(folder_name)
    rows = conn.execute("SELECT key FROM mail_sensitive_keys WHERE mailbox=?", (account,)).fetchall()
    known = {row["key"] for row in rows} | {sensitive_key(name) for name in D14_SENSITIVE_NAMES}
    return any(key == stored or key.startswith(stored + "/") or stored.startswith(key + "/") for stored in known)


def write_taxonomy(
    conn: sqlite3.Connection,
    account: str,
    folders: Iterable[Mapping[str, Any]],
    *,
    state: str = "proposed",
) -> int:
    """Write a taxonomy version, failing closed when a risk is missing."""

    payload: list[dict[str, str]] = []
    for folder in folders:
        name = str(folder.get("name", "")).strip()
        if not name:
            continue
        raw_risk = folder.get("risk")
        try:
            risk = Risk(raw_risk)
        except (ValueError, TypeError):
            risk = Risk.SENSITIVE
        payload.append(
            {
                "name": name,
                "risk": risk.value,
                "description": str(folder.get("description", "")),
            }
        )

    with conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM mail_taxonomy WHERE mailbox=?",
            (account,),
        ).fetchone()
        version = int(row["version"]) + 1
        conn.execute(
            "INSERT INTO mail_taxonomy(mailbox, version, state, json) VALUES (?, ?, ?, ?)",
            (account, version, state, json.dumps(payload, sort_keys=True)),
        )
        conn.execute(
            "UPDATE mail_mailboxes SET taxonomy_version=?, updated_at=? WHERE account=?",
            (version, iso(now_utc()), account),
        )
        for item in payload:
            if item["risk"] == Risk.SENSITIVE.value:
                conn.execute(
                    "INSERT INTO mail_sensitive_keys(mailbox, key, display_name, source, marked_at) "
                    "VALUES (?, ?, ?, 'operator', ?) ON CONFLICT(mailbox, key) DO NOTHING",
                    (account, sensitive_key(item["name"]), item["name"], iso(now_utc())),
                )
    return version


def approved_taxonomy(conn: sqlite3.Connection, account: str) -> list[TaxonomyFolder] | None:
    row = conn.execute(
        "SELECT json FROM mail_taxonomy WHERE mailbox=? AND state='approved' ORDER BY version DESC LIMIT 1",
        (account,),
    ).fetchone()
    if row is None:
        return None
    return [
        TaxonomyFolder(name=item["name"], risk=Risk(item["risk"]), description=item.get("description", ""))
        for item in json.loads(row["json"])
    ]
