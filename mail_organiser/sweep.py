"""The bounded sweep: replay durable work, then list and journal one page at a time.

The cursor advances when the page is journalled, not when the provider confirms.
A decision waiting for the operator, or deliberately not applied in a dry run,
is durable work. Replay reads the journal, never the provider window, so an
advanced cursor cannot lose it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import policy, rules, store
from .models import (
    FINAL_STATES,
    Decision,
    JournalEntry,
    JournalState,
    Message,
    Outcome,
    Ref,
    Rule,
    Scope,
    SweepLimits,
)

#: How long an operator has to resolve a queued decision.
DECISION_TTL = timedelta(days=14)

#: How long a failed action waits before replay.
RETRY_BACKOFF = timedelta(minutes=5)

#: Gate reasons an operator can resolve, so they get an ask row.
ASK_REASONS = frozenset({"ask", "handoff", "sensitive_target"})

#: A classifier takes the residue of one page and returns one decision per message.
Classifier = Callable[[tuple[Message, ...]], Sequence[Decision | None]]

COUNTER_FIELDS: tuple[str, ...] = (
    "listed",
    "journalled",
    "applied",
    "noop",
    "dry_run",
    "queued",
    "blocked",
    "retryable",
    "inflight",
    "waiting",
    "unknown",
    "unmatched",
    "errors",
)


class SweepError(Exception):
    """The adapter or the classifier broke its contract."""


@dataclass(frozen=True)
class SweepReport:
    """What one run did, in numbers the briefing and the tool can print."""

    mailbox: str
    sweep_id: str
    listed: int = 0
    journalled: int = 0
    applied: int = 0
    noop: int = 0
    dry_run: int = 0
    queued: int = 0
    blocked: int = 0
    retryable: int = 0
    inflight: int = 0
    waiting: int = 0
    unknown: int = 0
    unmatched: int = 0
    errors: int = 0
    stopped: str | None = None

    def counters(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in COUNTER_FIELDS} | {"stopped": self.stopped}

    def summary(self) -> str:
        parts = [f"{self.listed} listed", f"{self.applied} applied"]
        for count, label in (
            (self.queued, "queued"),
            (self.dry_run, "dry run"),
            (self.blocked, "blocked"),
            (self.retryable, "retryable"),
            (self.unmatched, "unmatched"),
            (self.errors, "errors"),
        ):
            if count:
                parts.append(f"{count} {label}")
        if self.stopped:
            parts.append(f"stopped: {self.stopped}")
        return f"mail sweep {self.mailbox}: " + ", ".join(parts)


def content_hash(message: Message) -> str:
    """What the decision saw. It says whether a decision is current, nothing more."""

    payload = json.dumps(
        {
            "sender": message.sender,
            "domain": message.sender_domain,
            "subject": message.subject,
            "body": message.body_text,
            "folder_ids": list(message.folder_ids),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sweep(
    conn: sqlite3.Connection,
    adapter: Any,
    account: str,
    *,
    limits: SweepLimits | None = None,
    classifier: Classifier | None = None,
    job_id: str = "manual",
    scheduled_for: datetime | None = None,
    execution_id: str | None = None,
    now: datetime | None = None,
    sweep_id: str | None = None,
) -> SweepReport:
    """One bounded run for one mailbox. One sweep owns the mailbox.

    Raises ``store.SweepBusy`` when another holder has the lease, or when this
    cron occurrence already ran.
    """

    limits = limits or SweepLimits()
    moment = now or store.now_utc()
    run_id = sweep_id or uuid.uuid4().hex[:12]
    box = store.mailbox(conn, account)
    fence = store.take_lease(conn, account, run_id, now=moment)

    tally: dict[str, Any] = dict.fromkeys(COUNTER_FIELDS, 0)
    tally["stopped"] = None
    started = False
    status = "failed"
    try:
        store.start_sweep(
            conn,
            sweep_id=run_id,
            job_id=job_id,
            scheduled_for=store.iso(scheduled_for or moment),
            mailbox=account,
            execution_id=execution_id,
            now=moment,
        )
        started = True
        tally["stopped"] = _run(conn, adapter, account, box, limits, classifier, tally, moment)
        status = "partial" if tally["errors"] or tally["stopped"] == "consecutive_errors" else "ok"
    finally:
        if started:
            store.finish_sweep(conn, run_id, status=status, counters=tally, now=moment)
        store.release_lease(conn, account, run_id, fence)

    return SweepReport(mailbox=account, sweep_id=run_id, **tally)


def _run(
    conn: sqlite3.Connection,
    adapter: Any,
    account: str,
    box: store.MailboxRow,
    limits: SweepLimits,
    classifier: Classifier | None,
    tally: dict[str, Any],
    moment: datetime,
) -> str | None:
    if not box.enabled:
        return "disabled"
    if box.paused:
        return "paused"

    processed = 0
    consecutive = 0

    # Durable work first. A row that waited for an operator, a dry run, or a
    # backoff is replayed from the journal, not from the provider window.
    for row in store.open_messages(conn, account):
        state = JournalState(row["state"])
        if state is JournalState.NEEDS_DECISION:
            tally["waiting"] += 1
            continue
        if state is JournalState.INFLIGHT:
            tally["inflight"] += 1
            continue
        if state is JournalState.RETRYABLE and _not_due(row, moment):
            tally["retryable"] += 1
            continue
        processed += 1
        outcome = _settle(conn, adapter, account, row, limits, moment, tally)
        consecutive = consecutive + 1 if outcome is Outcome.ERROR else 0
        if consecutive >= limits.consecutive_errors:
            return "consecutive_errors"
        if processed >= limits.per_mailbox:
            return "budget"

    cursor = box.cursor
    while processed < limits.per_mailbox:
        page = adapter.list_since(cursor, limits.page, Scope.INBOX)
        if page.scope is not Scope.INBOX:
            raise SweepError(f"adapter returned {page.scope} for an inbox sweep")
        if not page.messages:
            return None

        ruleset = store.enabled_rules(conn, account)
        decisions = _decide(tuple(page.messages), ruleset, classifier, limits.classify_batch)
        tally["listed"] += len(page.messages)

        if any(decision is None for decision in decisions):
            # Without a decision the page cannot be journalled, and the cursor
            # may not pass an unjournalled message.
            tally["unmatched"] += sum(1 for decision in decisions if decision is None)
            return "unmatched"

        versions = {rule.id: rule.version for rule in ruleset}
        entries = [
            _entry(account, message, decision, box, versions)
            for message, decision in zip(page.messages, decisions, strict=True)
            if decision is not None
        ]
        tally["journalled"] += store.commit_page(
            conn,
            account,
            entries,
            cursor=page.next_cursor,
            issued_at=moment,
            now=moment,
        )

        for message in page.messages:
            row = store.message_row(conn, account, message.ref.message_id)
            state = JournalState(row["state"])
            if state in FINAL_STATES:
                continue
            if state is JournalState.INFLIGHT:
                tally["inflight"] += 1
                continue
            if state is JournalState.NEEDS_DECISION:
                tally["waiting"] += 1
                continue
            processed += 1
            outcome = _settle(conn, adapter, account, row, limits, moment, tally)
            consecutive = consecutive + 1 if outcome is Outcome.ERROR else 0
            if consecutive >= limits.consecutive_errors:
                return "consecutive_errors"
            if processed >= limits.per_mailbox:
                return "budget"

        if page.complete or not page.next_cursor:
            return None
        if page.next_cursor == cursor:
            raise SweepError("adapter returned the same cursor")
        cursor = page.next_cursor

    return "budget"


def _not_due(row: sqlite3.Row, moment: datetime) -> bool:
    retry_after = row["retry_after"]
    return retry_after is not None and retry_after > store.iso(moment)


def _decide(
    messages: tuple[Message, ...],
    ruleset: list[Rule],
    classifier: Classifier | None,
    batch: int,
) -> list[Decision | None]:
    decided: list[Decision | None] = [rules.decision_for(ruleset, message) for message in messages]
    if classifier is None or all(decision is not None for decision in decided):
        return decided

    index = 0
    while index < len(messages):
        if decided[index] is not None:
            index += 1
            continue
        positions: list[int] = []
        cursor = index
        while cursor < len(messages) and len(positions) < batch:
            if decided[cursor] is None:
                positions.append(cursor)
            cursor += 1
        chunk = tuple(messages[position] for position in positions)
        answers = classifier(chunk)
        if len(answers) != len(chunk):
            raise SweepError(f"classifier returned {len(answers)} decisions for {len(chunk)} messages")
        for position, answer in zip(positions, answers, strict=True):
            decided[position] = answer
        index = cursor

    return decided


def _entry(
    account: str,
    message: Message,
    decision: Decision,
    box: store.MailboxRow,
    versions: dict[str, int],
) -> JournalEntry:
    return JournalEntry(
        ref=message.ref,
        content_hash=content_hash(message),
        decision=decision,
        state=JournalState.PENDING,
        decided_by="classifier" if decision.rule_id is None else f"rule:{decision.rule_id}",
        policy_version=box.policy_version,
        taxonomy_version=box.taxonomy_version,
        rule_id=decision.rule_id,
        rule_version=versions.get(decision.rule_id) if decision.rule_id else None,
    )


def _queue(
    conn: sqlite3.Connection,
    account: str,
    row: sqlite3.Row,
    decision: Decision,
    moment: datetime,
) -> None:
    store.open_decision(
        conn,
        account=account,
        message_id=row["message_id"],
        stable_id=row["stable_id"],
        decision=decision,
        expires_at=moment + DECISION_TTL,
        now=moment,
    )


def _settle(
    conn: sqlite3.Connection,
    adapter: Any,
    account: str,
    row: sqlite3.Row,
    limits: SweepLimits,
    moment: datetime,
    tally: dict[str, Any],
) -> Outcome | None:
    """Gate, then apply, then journal the outcome for one row."""

    message_id = row["message_id"]
    state = JournalState(row["state"])
    decision = Decision.from_json(row["decision_json"])

    gate = policy.apply_gate(conn, account, message_id)
    if not gate.allow:
        if gate.state is not None and gate.state is not state:
            store.set_state(conn, account, message_id, gate.state, expected=state)
        if gate.reason == "dry_run":
            tally["dry_run"] += 1
        elif gate.reason == "policy_block":
            tally["blocked"] += 1
        if gate.reason in ASK_REASONS:
            _queue(conn, account, row, decision, moment)
            tally["queued"] += 1
        return None

    if not store.set_state(conn, account, message_id, JournalState.INFLIGHT, expected=state):
        return None

    ref = Ref(mailbox=account, message_id=message_id, stable_id=row["stable_id"])
    result = adapter.apply(ref, decision)

    if result.outcome in (Outcome.APPLIED, Outcome.NOOP):
        if result.post_ref and result.post_ref != message_id:
            store.record_message_id(conn, account, message_id, result.post_ref)
        store.set_state(
            conn,
            account,
            message_id,
            JournalState.CONFIRMED,
            expected=JournalState.INFLIGHT,
            applied_at=moment,
        )
        tally["applied" if result.outcome is Outcome.APPLIED else "noop"] += 1
        return result.outcome

    if result.outcome is Outcome.UNKNOWN:
        # The row stays inflight. Reconcile confirms against the provider by
        # stable id rather than retrying blind.
        tally["unknown"] += 1
        return result.outcome

    tally["errors"] += 1
    attempts = int(row["attempts"]) + 1
    if attempts >= limits.consecutive_errors:
        store.set_state(
            conn,
            account,
            message_id,
            JournalState.NEEDS_DECISION,
            expected=JournalState.INFLIGHT,
            attempts=attempts,
        )
        _queue(conn, account, row, decision, moment)
        tally["queued"] += 1
    else:
        store.set_state(
            conn,
            account,
            message_id,
            JournalState.RETRYABLE,
            expected=JournalState.INFLIGHT,
            attempts=attempts,
            retry_after=store.iso(moment + RETRY_BACKOFF),
        )
        tally["retryable"] += 1
    return result.outcome
