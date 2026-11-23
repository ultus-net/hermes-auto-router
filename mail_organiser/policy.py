"""The four-behaviour policy and the one gate before provider access.

Direct approval and reconciliation both call ``apply_gate``. It re-reads the
mailbox, the message, the policy version, and the taxonomy version, so a
decision made twelve days ago cannot apply against today's settings.

The gate answers, it does not write. The caller journals ``Gate.state`` when one
is set, then applies only when ``Gate.allow`` is true.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from . import store
from .models import (
    PERSONAL_DEFAULTS,
    WORK_DEFAULTS,
    Behaviour,
    Decision,
    JournalState,
    MailboxPolicy,
    PolicyAction,
    Verb,
)
from .rules import RANK_OPERATOR

ACTION_FOR_VERB: Mapping[Verb, PolicyAction] = {
    Verb.FILE: PolicyAction.FILE_LABEL,
    Verb.LABEL: PolicyAction.FILE_LABEL,
    Verb.STAR: PolicyAction.STAR_FLAG,
    Verb.FLAG: PolicyAction.STAR_FLAG,
    Verb.MARK_READ: PolicyAction.MARK_READ,
    Verb.TRASH: PolicyAction.TRASH,
}


@dataclass(frozen=True)
class Gate:
    """The answer for one decision at dispatch time."""

    allow: bool
    state: JournalState | None = None
    behaviour: Behaviour | None = None
    action: PolicyAction | None = None
    reason: str | None = None


def profile_defaults(profile: str) -> dict[PolicyAction, Behaviour]:
    """Defaults per mailbox profile. An unknown profile takes the stricter set."""

    if profile == "personal":
        return dict(PERSONAL_DEFAULTS)
    return dict(WORK_DEFAULTS)


def default_policy(profile: str) -> MailboxPolicy:
    return MailboxPolicy(behaviours=profile_defaults(profile))


def behaviour_for(policy: MailboxPolicy, action: PolicyAction) -> Behaviour:
    return policy.behaviour_for(action)


def action_for(verb: Verb) -> PolicyAction | None:
    """Map a verb to its policy family. ``noop`` has no family."""

    return ACTION_FOR_VERB.get(verb)


def _operator_authority(
    conn: sqlite3.Connection,
    account: str,
    row: sqlite3.Row,
    decided_by: str | None,
) -> bool:
    """Only an operator rule or an operator approval may reach a sensitive folder."""

    source = decided_by if decided_by is not None else (row["decided_by"] or "")
    if source == "operator" or source.startswith("operator:"):
        return True
    if not source.startswith("rule:"):
        return False

    rule_id = row["rule_id"] or source.split(":", 1)[1]
    rule = conn.execute(
        "SELECT rank, source FROM mail_rules WHERE id=? AND mailbox=?",
        (rule_id, account),
    ).fetchone()
    return rule is not None and rule["source"] == "operator" and int(rule["rank"]) >= RANK_OPERATOR


def apply_gate(
    conn: sqlite3.Connection,
    account: str,
    message_id: str,
    *,
    decided_by: str | None = None,
) -> Gate:
    """Decide whether one journalled decision may reach the provider.

    ``decided_by`` overrides the stored provenance for an approval path, so an
    operator approving a classifier decision counts as operator authority.
    """

    box = store.mailbox(conn, account)
    if not box.enabled:
        return Gate(allow=False, reason="mailbox_disabled")
    if box.paused:
        return Gate(allow=False, reason="paused")

    row = store.message_row(conn, account, message_id)
    if row["decision_json"] is None:
        return Gate(allow=False, reason="undecided")
    decision = Decision.from_json(row["decision_json"])

    action = action_for(decision.verb)
    if action is None:
        return Gate(allow=False, reason="noop")

    behaviour = box.policy.behaviour_for(action)
    if behaviour is Behaviour.BLOCK:
        return Gate(allow=False, state=JournalState.BLOCKED, behaviour=behaviour, action=action, reason="policy_block")

    if row["policy_version"] != box.policy_version:
        return Gate(
            allow=False,
            state=JournalState.NEEDS_DECISION,
            behaviour=behaviour,
            action=action,
            reason="stale_policy",
        )
    if row["taxonomy_version"] != box.taxonomy_version:
        return Gate(
            allow=False,
            state=JournalState.NEEDS_DECISION,
            behaviour=behaviour,
            action=action,
            reason="stale_taxonomy",
        )

    if action is PolicyAction.FILE_LABEL:
        # Filing stays disabled until a taxonomy row is approved, because the
        # target folder must exist before anything is moved into it.
        if store.approved_taxonomy(conn, account) is None:
            return Gate(
                allow=False,
                state=JournalState.NEEDS_DECISION,
                behaviour=behaviour,
                action=action,
                reason="taxonomy_not_approved",
            )
        if (
            decision.target
            and store.is_sensitive(conn, account, decision.target)
            and not _operator_authority(conn, account, row, decided_by)
        ):
            return Gate(
                allow=False,
                state=JournalState.NEEDS_DECISION,
                behaviour=behaviour,
                action=action,
                reason="sensitive_target",
            )

    if box.dry_run:
        return Gate(allow=False, state=JournalState.DRY_RUN, behaviour=behaviour, action=action, reason="dry_run")

    if behaviour in (Behaviour.ASK, Behaviour.HANDOFF):
        return Gate(
            allow=False,
            state=JournalState.NEEDS_DECISION,
            behaviour=behaviour,
            action=action,
            reason=behaviour.value,
        )

    return Gate(allow=True, behaviour=behaviour, action=action)
