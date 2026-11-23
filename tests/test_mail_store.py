"""Behaviour tests for the journal, its state machine, and its guards."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_organiser import store
from mail_organiser.models import (
    FINAL_STATES,
    OPEN_STATES,
    PERSONAL_DEFAULTS,
    WORK_DEFAULTS,
    Decision,
    JournalEntry,
    JournalState,
    Ref,
    Risk,
    Verb,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
FULLWIDTH_FINANCE = "".join(chr(ord(char) + 0xFEE0) for char in "finance")


def entry(
    message_id: str,
    stable_id: str,
    state: JournalState,
    *,
    mailbox: str = "personal",
    target: str = "Receipts",
) -> JournalEntry:
    return JournalEntry(
        ref=Ref(mailbox=mailbox, message_id=message_id, stable_id=stable_id),
        content_hash="hash-" + stable_id,
        decision=Decision(verb=Verb.FILE, target=target),
        state=state,
        decided_by="rule:r1",
        policy_version=0,
        taxonomy_version=1,
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = store.connect(":memory:")
        store.initialize(self.conn)
        store.ensure_mailbox(self.conn, "personal", "google", behaviours=PERSONAL_DEFAULTS)


class SchemaTest(Base):
    def test_manifest_is_idempotent_and_versioned(self) -> None:
        first = store.schema_manifest(self.conn)
        store.initialize(self.conn)
        self.assertEqual(store.schema_manifest(self.conn), first)
        self.assertEqual(store.schema_version(self.conn), store.SCHEMA_VERSION)
        self.assertIn("table:mail_messages", first)
        self.assertIn("index:mail_decisions_open", first)

    def test_state_machine_has_exactly_eight_states(self) -> None:
        self.assertEqual(len(JournalState), 8)
        self.assertEqual(
            set(JournalState),
            set(OPEN_STATES) | set(FINAL_STATES) | {JournalState.INVALID},
        )
        self.assertTrue(OPEN_STATES.isdisjoint(FINAL_STATES))


class CommitPageTest(Base):
    def test_commit_journals_and_advances_cursor(self) -> None:
        written = store.commit_page(self.conn, "personal", [entry("m1", "s1", JournalState.DRY_RUN)], cursor="c1")
        self.assertEqual(written, 1)
        self.assertEqual(store.mailbox(self.conn, "personal").cursor, "c1")
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "dry_run")

    def test_bad_entry_rolls_back_journal_and_cursor(self) -> None:
        good = entry("m1", "s1", JournalState.PENDING)
        bad = entry("m2", "s2", JournalState.CONFIRMED)
        with self.assertRaises(store.InvalidTransition):
            store.commit_page(self.conn, "personal", [good, bad], cursor="c1")
        self.assertIsNone(store.mailbox(self.conn, "personal").cursor)
        with self.assertRaises(store.MessageNotFound):
            store.message_row(self.conn, "personal", "m1")

    def test_relisting_keeps_the_first_decision(self) -> None:
        store.commit_page(self.conn, "personal", [entry("m1", "s1", JournalState.PENDING, target="A")])
        written = store.commit_page(self.conn, "personal", [entry("m1", "s1", JournalState.PENDING, target="B")])
        self.assertEqual(written, 0)
        decision = Decision.from_json(store.message_row(self.conn, "personal", "m1")["decision_json"])
        self.assertEqual(decision.target, "A")

    def test_identity_is_stable_id_across_a_provider_id_change(self) -> None:
        store.commit_page(self.conn, "personal", [entry("m1", "s1", JournalState.PENDING)])
        written = store.commit_page(self.conn, "personal", [entry("m2", "s1", JournalState.PENDING)])
        self.assertEqual(written, 0)
        count = self.conn.execute("SELECT COUNT(*) AS n FROM mail_messages").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_terminal_state_cannot_reopen(self) -> None:
        store.commit_page(self.conn, "personal", [entry("m1", "s1", JournalState.PENDING)])
        self.assertTrue(store.set_state(self.conn, "personal", "m1", JournalState.INFLIGHT))
        self.assertTrue(store.set_state(self.conn, "personal", "m1", JournalState.CONFIRMED))
        with self.assertRaises(store.InvalidTransition):
            store.set_state(self.conn, "personal", "m1", JournalState.PENDING)

    def test_compare_and_set_refuses_a_stale_caller(self) -> None:
        store.commit_page(self.conn, "personal", [entry("m1", "s1", JournalState.PENDING)])
        self.assertFalse(
            store.set_state(
                self.conn,
                "personal",
                "m1",
                JournalState.INFLIGHT,
                expected=JournalState.CONFIRMED,
            )
        )


class LeaseTest(Base):
    def test_takeover_increments_the_fence_after_expiry(self) -> None:
        self.assertEqual(store.take_lease(self.conn, "personal", "sweep-a", ttl_seconds=300, now=T0), 1)
        with self.assertRaises(store.SweepBusy):
            store.take_lease(self.conn, "personal", "sweep-b", ttl_seconds=300, now=T0)
        fence = store.take_lease(
            self.conn,
            "personal",
            "sweep-b",
            ttl_seconds=300,
            now=T0 + timedelta(seconds=301),
        )
        self.assertEqual(fence, 2)

    def test_release_requires_the_current_fence(self) -> None:
        store.take_lease(self.conn, "personal", "sweep-a", now=T0)
        self.assertFalse(store.release_lease(self.conn, "personal", "sweep-a", fencing=99))
        self.assertTrue(store.release_lease(self.conn, "personal", "sweep-a", fencing=1))


class DecisionTest(Base):
    def test_open_decision_is_unique_per_stable_id(self) -> None:
        decision = Decision(verb=Verb.MARK_READ)
        first, created = store.open_decision(
            self.conn,
            account="personal",
            message_id="m1",
            stable_id="s1",
            decision=decision,
            expires_at=T0 + timedelta(days=14),
            now=T0,
        )
        second, created_again = store.open_decision(
            self.conn,
            account="personal",
            message_id="m1",
            stable_id="s1",
            decision=decision,
            expires_at=T0 + timedelta(days=14),
            now=T0,
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)

    def test_resolution_is_single_use(self) -> None:
        decision_id, _ = store.open_decision(
            self.conn,
            account="personal",
            message_id="m1",
            stable_id="s1",
            decision=Decision(verb=Verb.MARK_READ),
            expires_at=T0 + timedelta(days=14),
            now=T0,
        )
        self.assertTrue(store.resolve_decision(self.conn, decision_id, "approved", now=T0))
        self.assertFalse(store.resolve_decision(self.conn, decision_id, "denied", now=T0))

    def test_expired_decision_cannot_resolve(self) -> None:
        decision_id, _ = store.open_decision(
            self.conn,
            account="personal",
            message_id="m1",
            stable_id="s1",
            decision=Decision(verb=Verb.MARK_READ),
            expires_at=T0 - timedelta(seconds=1),
            now=T0 - timedelta(days=1),
        )
        self.assertFalse(store.resolve_decision(self.conn, decision_id, "approved", now=T0))


class SensitiveTest(Base):
    def test_d14_names_survive_case_width_and_nesting(self) -> None:
        store.seed_sensitive(self.conn, "personal")
        for name in ("Finance", FULLWIDTH_FINANCE, "Finance/Invoices", "Legal/2026/Contracts"):
            with self.subTest(name=name):
                self.assertTrue(store.is_sensitive(self.conn, "personal", name))
        self.assertFalse(store.is_sensitive(self.conn, "personal", "Receipts"))

    def test_taxonomy_without_risk_fails_closed(self) -> None:
        store.write_taxonomy(self.conn, "personal", [{"name": "Receipts"}], state="approved")
        folders = store.approved_taxonomy(self.conn, "personal")
        self.assertIsNotNone(folders)
        self.assertEqual(folders[0].risk, Risk.SENSITIVE)
        self.assertTrue(store.is_sensitive(self.conn, "personal", "Receipts"))

    def test_taxonomy_approval_advances_the_mailbox_version(self) -> None:
        self.assertEqual(store.mailbox(self.conn, "personal").taxonomy_version, 0)
        version = store.write_taxonomy(self.conn, "personal", [{"name": "Receipts", "risk": "low"}], state="approved")
        self.assertEqual(store.mailbox(self.conn, "personal").taxonomy_version, version)

    def test_d14_names_are_sensitive_before_any_seed(self) -> None:
        self.assertTrue(store.is_sensitive(self.conn, "personal", "Finance"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM mail_sensitive_keys").fetchone()["n"],
            0,
        )


class RulesTest(Base):
    def test_precedence_is_rank_then_priority(self) -> None:
        store.insert_rule(
            self.conn,
            rule_id="op",
            account="personal",
            matcher={"domain": "example.com"},
            target="A",
            source="operator",
            rank=40,
            priority=50,
        )
        store.insert_rule(
            self.conn,
            rule_id="learned",
            account="personal",
            matcher={"domain": "example.com"},
            target="B",
            source="learned",
            rank=30,
            priority=1,
        )
        store.insert_rule(
            self.conn,
            rule_id="classifier",
            account="personal",
            matcher={"domain": "example.com"},
            target="C",
            source="classifier",
            rank=20,
            priority=0,
        )
        self.assertEqual([rule.id for rule in store.enabled_rules(self.conn, "personal")], ["op", "learned", "classifier"])


class PolicyTest(unittest.TestCase):
    def test_work_asks_before_marking_read(self) -> None:
        from mail_organiser.models import MailboxPolicy, PolicyAction

        personal = MailboxPolicy(behaviours=PERSONAL_DEFAULTS)
        work = MailboxPolicy(behaviours=WORK_DEFAULTS)
        self.assertEqual(personal.behaviour_for(PolicyAction.MARK_READ).value, "auto")
        self.assertEqual(work.behaviour_for(PolicyAction.MARK_READ).value, "ask")
        self.assertEqual(work.behaviour_for(PolicyAction.TRASH).value, "handoff")

    def test_unknown_policy_key_is_ignored(self) -> None:
        policy = store.decode_policy('{"mark_read": "ask", "nonsense": "auto"}')
        from mail_organiser.models import PolicyAction

        self.assertEqual(policy.behaviour_for(PolicyAction.MARK_READ).value, "ask")
        self.assertEqual(policy.behaviour_for(PolicyAction.TRASH).value, "ask")


if __name__ == "__main__":
    unittest.main()
