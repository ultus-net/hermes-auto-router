"""Frozen values and enums that cross every mail boundary.

The journal has exactly eight message states. Business code must not invent a
ninth, and ``noop`` is an action outcome rather than a state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Scope(StrEnum):
    """Which mailbox window a page covers."""

    INBOX = "inbox"
    ARCHIVE = "archive"


class JournalState(StrEnum):
    """The eight states a ``mail_messages`` row may hold."""

    PENDING = "pending"
    INFLIGHT = "inflight"
    CONFIRMED = "confirmed"
    RETRYABLE = "retryable"
    NEEDS_DECISION = "needs_decision"
    DRY_RUN = "dry_run"
    BLOCKED = "blocked"
    INVALID = "invalid"


#: States that still owe work and are replayed by reconcile.
OPEN_STATES: frozenset[JournalState] = frozenset(
    {
        JournalState.PENDING,
        JournalState.INFLIGHT,
        JournalState.RETRYABLE,
        JournalState.NEEDS_DECISION,
        JournalState.DRY_RUN,
    }
)

#: Terminal states. A row in one of these is never replayed.
FINAL_STATES: frozenset[JournalState] = frozenset(
    {JournalState.CONFIRMED, JournalState.BLOCKED}
)


class Verb(StrEnum):
    """An action a decision can request."""

    FILE = "file"
    LABEL = "label"
    STAR = "star"
    FLAG = "flag"
    MARK_READ = "mark_read"
    TRASH = "trash"
    NOOP = "noop"


class Risk(StrEnum):
    """A taxonomy folder's reach for the unattended classifier."""

    LOW = "low"
    SENSITIVE = "sensitive"


class Behaviour(StrEnum):
    """The four-behaviour policy for one action type."""

    AUTO = "auto"
    ASK = "ask"
    HANDOFF = "handoff"
    BLOCK = "block"


class PolicyAction(StrEnum):
    """Action families as the policy table groups them."""

    FILE_LABEL = "file_label"
    STAR_FLAG = "star_flag"
    MARK_READ = "mark_read"
    TRASH = "trash"
    CREATE_RULE = "create_rule"


class Outcome(StrEnum):
    """The provider outcome of one apply call."""

    APPLIED = "applied"
    NOOP = "noop"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Ref:
    """A message's mailbox, current provider id, and immutable identity."""

    mailbox: str
    message_id: str
    stable_id: str


@dataclass(frozen=True)
class Message:
    """One listed message, carrying only what the classifier may see."""

    ref: Ref
    sender: str
    sender_domain: str
    subject: str
    folder_ids: tuple[str, ...] = ()
    list_id: str | None = None
    body_text: str = ""
    internet_message_id: str | None = None
    received_at: str | None = None


@dataclass(frozen=True)
class Folder:
    """A provider folder or label."""

    name: str
    folder_id: str


@dataclass(frozen=True)
class Decision:
    """What to do with one message, independent of who decided it."""

    verb: Verb
    target: str | None = None
    reason: str | None = None
    rule_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "verb": self.verb.value,
                "target": self.target,
                "reason": self.reason,
                "rule_id": self.rule_id,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> Decision:
        data = json.loads(raw)
        return cls(
            verb=Verb(data["verb"]),
            target=data.get("target"),
            reason=data.get("reason"),
            rule_id=data.get("rule_id"),
        )


@dataclass(frozen=True)
class ApplyResult:
    """The adapter's answer for one decision."""

    outcome: Outcome
    post_ref: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Page:
    """One bounded provider page. The page carries its own cursor."""

    messages: tuple[Message, ...]
    scope: Scope
    next_cursor: str | None
    high_water: str | None = None
    complete: bool = True


@dataclass(frozen=True)
class Rule:
    """A deterministic matcher and its target folder."""

    id: str
    mailbox: str
    matcher: dict[str, Any]
    target: str
    rank: int
    priority: int
    source: str
    version: int = 1
    enabled: bool = True


@dataclass(frozen=True)
class JournalEntry:
    """One decided message, ready to commit as a page."""

    ref: Ref
    content_hash: str
    decision: Decision
    state: JournalState
    decided_by: str
    policy_version: int
    taxonomy_version: int
    rule_id: str | None = None
    rule_version: int | None = None
    validator_version: int = 1


@dataclass(frozen=True)
class SweepLimits:
    """Bounded work per sweep, straight from the design."""

    page: int = 100
    per_mailbox: int = 200
    classify_batch: int = 25
    consecutive_errors: int = 3


@dataclass(frozen=True)
class MailboxPolicy:
    """The four-behaviour defaults for one mailbox profile."""

    behaviours: dict[PolicyAction, Behaviour] = field(default_factory=dict)
    version: int = 0

    def behaviour_for(self, action: PolicyAction) -> Behaviour:
        return self.behaviours.get(action, Behaviour.ASK)


PERSONAL_DEFAULTS: dict[PolicyAction, Behaviour] = {
    PolicyAction.FILE_LABEL: Behaviour.AUTO,
    PolicyAction.STAR_FLAG: Behaviour.AUTO,
    PolicyAction.MARK_READ: Behaviour.AUTO,
    PolicyAction.TRASH: Behaviour.HANDOFF,
    PolicyAction.CREATE_RULE: Behaviour.ASK,
}

WORK_DEFAULTS: dict[PolicyAction, Behaviour] = {
    PolicyAction.FILE_LABEL: Behaviour.AUTO,
    PolicyAction.STAR_FLAG: Behaviour.AUTO,
    PolicyAction.MARK_READ: Behaviour.ASK,
    PolicyAction.TRASH: Behaviour.HANDOFF,
    PolicyAction.CREATE_RULE: Behaviour.ASK,
}

#: D14, verbatim. The sensitive set is named, never inferred at decision time.
D14_SENSITIVE_NAMES: tuple[str, ...] = (
    "finance",
    "legal",
    "hr",
    "contracts",
    "invoices",
)
