"""Behaviour tests for the rule engine, learning, and the policy gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_organiser import policy, rules, store
from mail_organiser.models import (
    PERSONAL_DEFAULTS,
    WORK_DEFAULTS,
    Behaviour,
    Decision,
    JournalEntry,
    JournalState,
    Message,
    PolicyAction,
    Ref,
    Verb,
)

RECEIPTS = [{"name": "Receipts", "risk": "low"}]


def message(message_id: str, *, subject: str, domain: str = "github.com") -> Message:
    return Message(
        ref=Ref(mailbox="personal", message_id=message_id, stable_id=message_id),
        sender=f"bot@{domain}",
        sender_domain=domain,
        subject=subject,
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = store.connect(":memory:")
        store.initialize(self.conn)
        store.ensure_mailbox(self.conn, "personal", "google", behaviours=PERSONAL_DEFAULTS, dry_run=False)

    def tearDown(self) -> None:
        self.conn.close()

    def approve_taxonomy(self, folders: list[dict[str, Any]] | None = None) -> int:
        return store.write_taxonomy(self.conn, "personal", folders or RECEIPTS, state="approved")

    def journal(
        self,
        message_id: str,
        verb: Verb,
        *,
        mailbox: str = "personal",
        target: str | None = None,
        decided_by: str = "classifier",
        rule_id: str | None = None,
    ) -> None:
        box = store.mailbox(self.conn, mailbox)
        store.commit_page(
            self.conn,
            mailbox,
            [
                JournalEntry(
                    ref=Ref(mailbox=mailbox, message_id=message_id, stable_id=message_id),
                    content_hash=f"hash-{message_id}",
                    decision=Decision(verb=verb, target=target),
                    state=JournalState.PENDING,
                    decided_by=decided_by,
                    policy_version=box.policy_version,
                    taxonomy_version=box.taxonomy_version,
                    rule_id=rule_id,
                )
            ],
        )


class SuggestionTest(Base):
    def test_a_correction_produces_a_suggestion(self) -> None:
        first = message("m1", subject="Re: Your receipt from GitHub (#1234)")
        second = message("m2", subject="Your receipt from GitHub (#5678)")
        third = message("m3", subject="Your receipt from GitHub (#9999)")

        self.assertIsNone(rules.record_correction(self.conn, "personal", first, "Receipts"))
        self.assertEqual(rules.list_suggestions(self.conn, "personal"), [])

        key = rules.record_correction(self.conn, "personal", second, "Receipts")
        self.assertIsNotNone(key)
        suggestions = rules.list_suggestions(self.conn, "personal")
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].key, key)
        self.assertEqual(suggestions[0].count, 2)
        self.assertEqual(suggestions[0].target, "Receipts")

        self.assertTrue(rules.dismiss_suggestion(self.conn, "personal", key))
        self.assertEqual(rules.list_suggestions(self.conn, "personal"), [])
        self.assertIsNone(rules.record_correction(self.conn, "personal", third, "Receipts"))
        self.assertEqual(rules.list_suggestions(self.conn, "personal"), [])

    def test_different_subject_stems_do_not_merge(self) -> None:
        self.assertIsNone(
            rules.record_correction(self.conn, "personal", message("m1", subject="Invoice 1"), "Receipts")
        )
        self.assertIsNone(
            rules.record_correction(self.conn, "personal", message("m2", subject="Build failed"), "Receipts")
        )
        self.assertEqual(rules.list_suggestions(self.conn, "personal"), [])

    def test_a_covered_message_records_nothing(self) -> None:
        store.insert_rule(
            self.conn,
            rule_id="op",
            account="personal",
            matcher={"domain": "github.com"},
            target="Receipts",
            source="operator",
            rank=rules.RANK_OPERATOR,
            priority=1,
        )
        for index in range(2):
            self.assertIsNone(
                rules.record_correction(self.conn, "personal", message(f"m{index}", subject="Your receipt"), "Receipts")
            )
        self.assertEqual(rules.list_suggestions(self.conn, "personal"), [])

    def test_a_sensitive_target_never_suggests(self) -> None:
        for index in range(2):
            self.assertIsNone(
                rules.record_correction(self.conn, "personal", message(f"m{index}", subject="Tax invoice"), "Finance")
            )
        self.assertEqual(rules.list_suggestions(self.conn, "personal"), [])

    def test_accept_writes_a_learned_rule_and_undo_restores_it(self) -> None:
        self.approve_taxonomy()
        rules.record_correction(self.conn, "personal", message("m1", subject="Your receipt (#1)"), "Receipts")
        key = rules.record_correction(self.conn, "personal", message("m2", subject="Your receipt (#2)"), "Receipts")
        self.assertIsNotNone(key)

        rule = rules.accept_suggestion(self.conn, "personal", key)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.rank, rules.RANK_LEARNED)
        self.assertEqual(rule.source, "learned")
        self.assertEqual([item.id for item in store.enabled_rules(self.conn, "personal")], [rule.id])
        self.assertEqual(rules.list_suggestions(self.conn, "personal"), [])

        self.journal("m1", Verb.FILE, target="Receipts", decided_by=f"rule:{rule.id}", rule_id=rule.id)
        before = store.mailbox(self.conn, "personal").ruleset_generation

        receipt = rules.delete_rule(self.conn, "personal", rule.id)
        self.assertIsNotNone(receipt)
        self.assertIsNone(rules.delete_rule(self.conn, "personal", rule.id))
        self.assertIsNone(store.message_row(self.conn, "personal", "m1")["rule_id"])
        self.assertEqual(store.enabled_rules(self.conn, "personal"), [])
        self.assertGreater(store.mailbox(self.conn, "personal").ruleset_generation, before)

        restored = rules.undo_delete(self.conn, "personal", receipt)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.id, rule.id)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["rule_id"], rule.id)
        self.assertIsNone(rules.undo_delete(self.conn, "personal", receipt))

    def test_undo_re_links_only_rows_still_as_the_delete_left_them(self) -> None:
        self.approve_taxonomy()
        rules.record_correction(self.conn, "personal", message("m1", subject="Your receipt (#1)"), "Receipts")
        key = rules.record_correction(self.conn, "personal", message("m2", subject="Your receipt (#2)"), "Receipts")
        rule = rules.accept_suggestion(self.conn, "personal", key)
        self.assertIsNotNone(rule)

        self.journal("m1", Verb.FILE, target="Receipts", decided_by=f"rule:{rule.id}", rule_id=rule.id)
        self.journal("m2", Verb.FILE, target="Receipts", decided_by=f"rule:{rule.id}", rule_id=rule.id)
        receipt = rules.delete_rule(self.conn, "personal", rule.id)
        self.assertIsNotNone(receipt)

        with self.conn:
            self.conn.execute("UPDATE mail_messages SET rule_id='other' WHERE mailbox='personal' AND message_id='m2'")
        rules.undo_delete(self.conn, "personal", receipt)

        self.assertEqual(store.message_row(self.conn, "personal", "m1")["rule_id"], rule.id)
        self.assertEqual(store.message_row(self.conn, "personal", "m2")["rule_id"], "other")


class GateTest(Base):
    def test_behaviour_for_applies_defaults_per_mailbox(self) -> None:
        personal = policy.default_policy("personal")
        work = policy.default_policy("work")

        self.assertEqual(policy.behaviour_for(personal, PolicyAction.MARK_READ), Behaviour.AUTO)
        self.assertEqual(policy.behaviour_for(work, PolicyAction.MARK_READ), Behaviour.ASK)
        self.assertEqual(policy.behaviour_for(work, PolicyAction.TRASH), Behaviour.HANDOFF)
        self.assertEqual(policy.behaviour_for(policy.default_policy("unknown"), PolicyAction.MARK_READ), Behaviour.ASK)

    def test_a_paused_mailbox_refuses_every_action(self) -> None:
        self.approve_taxonomy()
        store.set_paused(self.conn, "personal", True)
        self.journal("m1", Verb.FILE, target="Receipts")

        gate = policy.apply_gate(self.conn, "personal", "m1")
        self.assertFalse(gate.allow)
        self.assertIsNone(gate.state)
        self.assertEqual(gate.reason, "paused")

    def test_a_dry_run_journals_instead_of_applying(self) -> None:
        self.approve_taxonomy()
        store.set_dry_run(self.conn, "personal", True)
        self.journal("m1", Verb.FILE, target="Receipts")

        gate = policy.apply_gate(self.conn, "personal", "m1")
        self.assertFalse(gate.allow)
        self.assertEqual(gate.state, JournalState.DRY_RUN)
        self.assertEqual(gate.reason, "dry_run")

    def test_ask_and_handoff_queue_a_decision(self) -> None:
        store.ensure_mailbox(self.conn, "work", "google", behaviours=WORK_DEFAULTS, dry_run=False)
        self.journal("w1", Verb.MARK_READ, mailbox="work")
        self.journal("w2", Verb.TRASH, mailbox="work")

        ask = policy.apply_gate(self.conn, "work", "w1")
        self.assertFalse(ask.allow)
        self.assertEqual(ask.state, JournalState.NEEDS_DECISION)
        self.assertEqual(ask.reason, "ask")

        handoff = policy.apply_gate(self.conn, "work", "w2")
        self.assertFalse(handoff.allow)
        self.assertEqual(handoff.state, JournalState.NEEDS_DECISION)
        self.assertEqual(handoff.reason, "handoff")

    def test_block_refuses_and_journals_the_refusal(self) -> None:
        behaviours = dict(PERSONAL_DEFAULTS)
        behaviours[PolicyAction.TRASH] = Behaviour.BLOCK
        store.ensure_mailbox(self.conn, "strict", "google", behaviours=behaviours, dry_run=False)
        self.journal("s1", Verb.TRASH, mailbox="strict")

        gate = policy.apply_gate(self.conn, "strict", "s1")
        self.assertFalse(gate.allow)
        self.assertEqual(gate.state, JournalState.BLOCKED)
        self.assertEqual(gate.reason, "policy_block")

    def test_filing_stays_disabled_until_taxonomy_approval(self) -> None:
        self.journal("m1", Verb.FILE, target="Receipts")

        gate = policy.apply_gate(self.conn, "personal", "m1")
        self.assertFalse(gate.allow)
        self.assertEqual(gate.state, JournalState.NEEDS_DECISION)
        self.assertEqual(gate.reason, "taxonomy_not_approved")

    def test_classifier_cannot_file_into_a_sensitive_folder(self) -> None:
        self.approve_taxonomy()
        self.journal("m1", Verb.FILE, target="Finance", decided_by="classifier")

        gate = policy.apply_gate(self.conn, "personal", "m1")
        self.assertFalse(gate.allow)
        self.assertEqual(gate.state, JournalState.NEEDS_DECISION)
        self.assertEqual(gate.reason, "sensitive_target")

    def test_operator_rule_may_file_into_a_sensitive_folder(self) -> None:
        self.approve_taxonomy([{"name": "Finance", "risk": "sensitive"}])
        store.insert_rule(
            self.conn,
            rule_id="op",
            account="personal",
            matcher={"domain": "bank.example"},
            target="Finance",
            source="operator",
            rank=rules.RANK_OPERATOR,
            priority=1,
        )
        self.journal("m1", Verb.FILE, target="Finance", decided_by="rule:op", rule_id="op")

        gate = policy.apply_gate(self.conn, "personal", "m1")
        self.assertTrue(gate.allow)
        self.assertEqual(gate.action, PolicyAction.FILE_LABEL)

    def test_operator_approval_overrides_classifier_provenance(self) -> None:
        self.approve_taxonomy()
        self.journal("m1", Verb.FILE, target="Finance", decided_by="classifier")

        gate = policy.apply_gate(self.conn, "personal", "m1", decided_by="operator")
        self.assertTrue(gate.allow)

    def test_a_learned_rule_cannot_file_into_a_sensitive_folder(self) -> None:
        self.approve_taxonomy([{"name": "Finance", "risk": "sensitive"}])
        store.insert_rule(
            self.conn,
            rule_id="learned",
            account="personal",
            matcher={"domain": "bank.example"},
            target="Finance",
            source="learned",
            rank=rules.RANK_LEARNED,
            priority=100,
        )
        self.journal("m1", Verb.FILE, target="Finance", decided_by="rule:learned", rule_id="learned")

        gate = policy.apply_gate(self.conn, "personal", "m1")
        self.assertFalse(gate.allow)
        self.assertEqual(gate.reason, "sensitive_target")

    def test_a_stale_policy_version_forces_a_decision(self) -> None:
        self.approve_taxonomy()
        self.journal("m1", Verb.FILE, target="Receipts")
        with self.conn:
            self.conn.execute("UPDATE mail_mailboxes SET policy_version=policy_version+1 WHERE account='personal'")

        gate = policy.apply_gate(self.conn, "personal", "m1")
        self.assertFalse(gate.allow)
        self.assertEqual(gate.state, JournalState.NEEDS_DECISION)
        self.assertEqual(gate.reason, "stale_policy")


if __name__ == "__main__":
    unittest.main()
