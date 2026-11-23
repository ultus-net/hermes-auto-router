"""Deterministic rules, learning from corrections, and one precedence number.

Operator 40, learned 30, classifier 20. A later classifier run cannot outrank a
learned rule, and a later learned rule cannot outrank an operator one.

A correction is evidence, never a silent rule. Two corrections of the same
matcher key produce a suggestion only when no enabled rule already covers the
message. Accepting writes a learned rule. Deleting writes a receipt, and the
receipt is the only input undo trusts.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import store
from .models import (
    OPEN_STATES,
    Decision,
    Message,
    Rule,
    Verb,
)

RANK_OPERATOR = 40
RANK_LEARNED = 30
RANK_CLASSIFIER = 20

#: How many hand filings of one key make a suggestion.
SUGGESTION_THRESHOLD = 2

_THREAD_PREFIX = re.compile(r"^(?:(?:re|fwd?|aw|sv)\s*(?:\[\d+\]|\d+)?\s*:\s*)+", re.IGNORECASE)
_BRACKETED_REFERENCE = re.compile(r"[\(\[]\s*#?\d+\s*[\)\]]")
_DIGITS = re.compile(r"\d+")

#: Matcher fields the engine understands. An unknown key matches nothing.
MATCHER_FIELDS: frozenset[str] = frozenset(
    {"domain", "sender", "subject_stem", "subject_contains", "list_id", "folder_id"}
)


class RuleError(Exception):
    """Base class for a refusal in the rule engine."""


class SensitiveTarget(RuleError):
    """A learned rule cannot hold authority over a sensitive folder."""


@dataclass(frozen=True)
class Suggestion:
    """One actionable suggestion with the filings that justify it."""

    mailbox: str
    key: str
    target: str
    matcher: dict[str, Any]
    evidence: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.evidence)


def normalise_text(value: str) -> str:
    """Fold width, case, and whitespace. The same key for the same text."""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def subject_stem(subject: str) -> str:
    """A subject with thread prefixes and reference numbers removed.

    Recurring mail differs only in its reference number, so the stem groups
    ``Invoice 4711`` with ``Invoice 4712`` and both with ``Re: Invoice [4711]``.
    """

    text = normalise_text(subject)
    text = _THREAD_PREFIX.sub("", text)
    text = _BRACKETED_REFERENCE.sub(" ", text)
    text = _DIGITS.sub("#", text)
    text = " ".join(text.split())
    return text.strip(" .,:;!-—_")


def matcher_for(message: Message) -> dict[str, str]:
    """The matcher a manual filing would generate: domain plus subject stem."""

    matcher: dict[str, str] = {}
    domain = normalise_text(message.sender_domain)
    if domain:
        matcher["domain"] = domain
    stem = subject_stem(message.subject)
    if stem:
        matcher["subject_stem"] = stem
    return matcher


def matcher_key(matcher: Mapping[str, Any]) -> str:
    """A readable, stable key for a suggestion and its dismissal."""

    domain = normalise_text(str(matcher.get("domain", "*")))
    stem = normalise_text(str(matcher.get("subject_stem", "*")))
    return f"{domain}:{stem}"


def rule_matches(matcher: Mapping[str, Any], message: Message) -> bool:
    """Every present field must match. An empty or unknown matcher matches nothing."""

    if not matcher:
        return False

    for field, raw in matcher.items():
        if field not in MATCHER_FIELDS:
            return False
        if field == "domain":
            if normalise_text(str(raw)) != normalise_text(message.sender_domain):
                return False
        elif field == "sender":
            if normalise_text(str(raw)) != normalise_text(message.sender):
                return False
        elif field == "subject_stem":
            if normalise_text(str(raw)) != subject_stem(message.subject):
                return False
        elif field == "subject_contains":
            if normalise_text(str(raw)) not in normalise_text(message.subject):
                return False
        elif field == "list_id":
            if message.list_id is None or normalise_text(str(raw)) != normalise_text(message.list_id):
                return False
        elif field == "folder_id" and str(raw) not in message.folder_ids:
            return False

    return True


def matching_rule(rules: Iterable[Rule], message: Message) -> Rule | None:
    """The first enabled rule in precedence order that matches."""

    for rule in rules:
        if rule.enabled and rule_matches(rule.matcher, message):
            return rule
    return None


def decision_for(rules: Iterable[Rule], message: Message) -> Decision | None:
    """A filing decision from the highest-precedence matching rule."""

    rule = matching_rule(rules, message)
    if rule is None:
        return None
    return Decision(verb=Verb.FILE, target=rule.target, reason=f"rule {rule.id}", rule_id=rule.id)


def _evidence(raw: str) -> tuple[dict[str, Any], list[str]]:
    data = json.loads(raw)
    if isinstance(data, list):
        return {}, [str(item) for item in data]
    matcher = data.get("matcher") or {}
    message_ids = [str(item) for item in data.get("message_ids", [])]
    return dict(matcher), message_ids


def _same_matcher(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return {key: normalise_text(str(value)) for key, value in left.items()} == {
        key: normalise_text(str(value)) for key, value in right.items()
    }


def record_correction(
    conn: sqlite3.Connection,
    account: str,
    message: Message,
    target: str,
) -> str | None:
    """Record one hand filing. Returns the key once it becomes actionable.

    Nothing is recorded when an enabled rule already covers the message, or
    when the target is sensitive: a learned rule can never hold that authority,
    so a suggestion for it would be noise.

    The first filing stages evidence. The second raises the suggestion.
    """

    if store.is_sensitive(conn, account, target):
        return None

    matcher = matcher_for(message)
    if not matcher:
        return None
    if matching_rule(store.enabled_rules(conn, account), message) is not None:
        return None

    key = matcher_key(matcher)
    row = conn.execute(
        "SELECT target, evidence_json, dismissed_at FROM mail_rule_suggestions WHERE mailbox=? AND key=?",
        (account, key),
    ).fetchone()

    if row is not None and row["dismissed_at"] is not None:
        # A dismissal never returns, so later filings add nothing.
        return None

    if row is None:
        payload = {"matcher": matcher, "message_ids": [message.ref.message_id]}
        with conn:
            conn.execute(
                "INSERT INTO mail_rule_suggestions(mailbox, key, target, evidence_json) VALUES (?, ?, ?, ?)",
                (account, key, target, json.dumps(payload, sort_keys=True)),
            )
        return None

    stored_matcher, message_ids = _evidence(row["evidence_json"])
    if message.ref.message_id not in message_ids:
        message_ids.append(message.ref.message_id)
    payload = {"matcher": stored_matcher or matcher, "message_ids": message_ids}
    with conn:
        conn.execute(
            "UPDATE mail_rule_suggestions SET target=?, evidence_json=? WHERE mailbox=? AND key=?",
            (target, json.dumps(payload, sort_keys=True), account, key),
        )
    return key if len(message_ids) >= SUGGESTION_THRESHOLD else None


def list_suggestions(conn: sqlite3.Connection, account: str) -> list[Suggestion]:
    """The `/mail-rules suggestions` listing: actionable keys with their count."""

    rows = conn.execute(
        "SELECT key, target, evidence_json FROM mail_rule_suggestions "
        "WHERE mailbox=? AND dismissed_at IS NULL ORDER BY key",
        (account,),
    ).fetchall()
    suggestions: list[Suggestion] = []
    for row in rows:
        matcher, message_ids = _evidence(row["evidence_json"])
        if len(message_ids) < SUGGESTION_THRESHOLD:
            continue
        suggestions.append(
            Suggestion(
                mailbox=account,
                key=row["key"],
                target=row["target"],
                matcher=matcher,
                evidence=tuple(message_ids),
            )
        )
    return suggestions


def dismiss_suggestion(conn: sqlite3.Connection, account: str, key: str) -> bool:
    """`/mail-rules dismiss <key>`. A dismissal never returns."""

    with conn:
        result = conn.execute(
            "UPDATE mail_rule_suggestions SET dismissed_at=COALESCE(dismissed_at, ?) WHERE mailbox=? AND key=?",
            (store.iso(store.now_utc()), account, key),
        )
    return result.rowcount == 1


def accept_suggestion(
    conn: sqlite3.Connection,
    account: str,
    key: str,
    *,
    priority: int = 100,
) -> Rule | None:
    """Accept one suggestion, writing a learned rule at rank 30.

    Returns the covering rule when one landed since the suggestion appeared.
    The next sweep re-decides open rows through the new rule; nothing is
    re-filed from here, because no message content is stored.
    """

    row = conn.execute(
        "SELECT target, evidence_json, dismissed_at FROM mail_rule_suggestions WHERE mailbox=? AND key=?",
        (account, key),
    ).fetchone()
    if row is None or row["dismissed_at"] is not None:
        return None

    matcher, _ = _evidence(row["evidence_json"])
    if not matcher:
        return None
    if store.is_sensitive(conn, account, row["target"]):
        raise SensitiveTarget(f"{row['target']} is sensitive and needs an operator rule")

    for existing in store.enabled_rules(conn, account):
        if _same_matcher(existing.matcher, matcher):
            dismiss_suggestion(conn, account, key)
            return existing

    rule = Rule(
        id=f"learned-{secrets.token_hex(4)}",
        mailbox=account,
        matcher=matcher,
        target=row["target"],
        rank=RANK_LEARNED,
        priority=priority,
        source="learned",
    )
    store.insert_rule(
        conn,
        rule_id=rule.id,
        account=account,
        matcher=rule.matcher,
        target=rule.target,
        source=rule.source,
        rank=rule.rank,
        priority=rule.priority,
    )
    dismiss_suggestion(conn, account, key)
    return rule


def delete_rule(conn: sqlite3.Connection, account: str, rule_id: str) -> str | None:
    """Delete a rule and write the undo receipt.

    Open rows the rule owned are unlinked in the same transaction, and the
    receipt records them. Undo re-links only a row still as the delete left it.
    """

    row = conn.execute("SELECT * FROM mail_rules WHERE id=? AND mailbox=?", (rule_id, account)).fetchone()
    if row is None:
        return None

    states = [state.value for state in OPEN_STATES]
    placeholders = ", ".join("?" for _ in states)
    linked = [
        item["message_id"]
        for item in conn.execute(
            f"SELECT message_id FROM mail_messages WHERE mailbox=? AND rule_id=? AND state IN ({placeholders})",
            (account, rule_id, *states),
        ).fetchall()
    ]

    receipt = secrets.token_hex(6)
    payload = {
        "rule": {
            "id": row["id"],
            "matcher": json.loads(row["matcher_json"]),
            "target": row["target"],
            "rank": row["rank"],
            "priority": row["priority"],
            "source": row["source"],
            "version": row["version"],
            "enabled": bool(row["enabled"]),
        },
        "unlinked": linked,
    }
    with conn:
        conn.execute(
            f"UPDATE mail_messages SET rule_id=NULL WHERE mailbox=? AND rule_id=? AND state IN ({placeholders})",
            (account, rule_id, *states),
        )
        conn.execute("DELETE FROM mail_rules WHERE id=? AND mailbox=?", (rule_id, account))
        conn.execute(
            "INSERT INTO mail_rule_deletions(receipt, mailbox, rule_json, deleted_at) VALUES (?, ?, ?, ?)",
            (receipt, account, json.dumps(payload, sort_keys=True), store.iso(store.now_utc())),
        )
        store.bump_ruleset_generation(conn, account)
    return receipt


def undo_delete(conn: sqlite3.Connection, account: str, receipt: str) -> Rule | None:
    """`/mail-rules undo <receipt>`. The receipt is the only input undo trusts."""

    row = conn.execute(
        "SELECT rule_json FROM mail_rule_deletions WHERE receipt=? AND mailbox=?",
        (receipt, account),
    ).fetchone()
    if row is None:
        return None

    payload = json.loads(row["rule_json"])
    data = payload.get("rule", {})
    linked = payload.get("unlinked", [])
    rule = Rule(
        id=data["id"],
        mailbox=account,
        matcher=dict(data["matcher"]),
        target=data["target"],
        rank=int(data["rank"]),
        priority=int(data["priority"]),
        source=data["source"],
        version=int(data["version"]),
        enabled=bool(data.get("enabled", True)),
    )

    with conn:
        conn.execute(
            "INSERT INTO mail_rules"
            "(id, mailbox, matcher_json, target, rank, priority, source, version, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (
                rule.id,
                account,
                json.dumps(rule.matcher, sort_keys=True),
                rule.target,
                rule.rank,
                rule.priority,
                rule.source,
                rule.version,
                int(rule.enabled),
            ),
        )
        for message_id in linked:
            conn.execute(
                "UPDATE mail_messages SET rule_id=? WHERE mailbox=? AND message_id=? AND rule_id IS NULL",
                (rule.id, account, message_id),
            )
        conn.execute("DELETE FROM mail_rule_deletions WHERE receipt=?", (receipt,))
        store.bump_ruleset_generation(conn, account)
    return rule
