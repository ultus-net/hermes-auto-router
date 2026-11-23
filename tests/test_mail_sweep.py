"""Behaviour tests for the fake provider and the bounded sweep."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_organiser import store
from mail_organiser.fake import FakeMailbox
from mail_organiser.models import (
    PERSONAL_DEFAULTS,
    WORK_DEFAULTS,
    Decision,
    Folder,
    Message,
    Ref,
    Scope,
    Verb,
)
from mail_organiser.sweep import SweepError, content_hash, sweep

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def message(
    message_id: str,
    *,
    mailbox: str = "personal",
    domain: str = "github.com",
    subject: str = "Your receipt",
    body: str = "",
) -> Message:
    return Message(
        ref=Ref(mailbox=mailbox, message_id=message_id, stable_id=f"stable-{message_id}"),
        sender=f"bot@{domain}",
        sender_domain=domain,
        subject=subject,
        body_text=body,
        received_at=T0.isoformat(),
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = store.connect(":memory:")
        store.initialize(self.conn)
        store.ensure_mailbox(self.conn, "personal", "google", behaviours=PERSONAL_DEFAULTS, dry_run=False)
        store.write_taxonomy(self.conn, "personal", [{"name": "Receipts", "risk": "low"}], state="approved")

    def tearDown(self) -> None:
        self.conn.close()

    def add_rule(self, *, rule_id: str = "op", domain: str = "github.com", target: str = "Receipts") -> None:
        store.insert_rule(
            self.conn,
            rule_id=rule_id,
            account="personal",
            matcher={"domain": domain},
            target=target,
            source="operator",
            rank=40,
            priority=1,
        )

    def sweep(self, adapter: FakeMailbox, *, now: datetime = T0, classifier=None, mailbox: str = "personal"):
        return sweep(self.conn, adapter, mailbox, now=now, classifier=classifier)


class SweepTest(Base):
    def test_a_rules_sweep_files_matches_and_advances_the_cursor(self) -> None:
        self.add_rule()
        adapter = FakeMailbox([message("m1"), message("m2")])

        report = self.sweep(adapter)

        self.assertEqual(report.listed, 2)
        self.assertEqual(report.applied, 2)
        self.assertIn("2 applied", report.summary())
        self.assertEqual(store.mailbox(self.conn, "personal").cursor, "2")
        self.assertEqual([item.folder_ids for item in adapter.messages], [("Receipts",), ("Receipts",)])
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "confirmed")

    def test_an_unmatched_page_is_not_journalled_and_the_cursor_stays(self) -> None:
        adapter = FakeMailbox([message("m1", domain="unknown.example")])

        report = self.sweep(adapter)

        self.assertEqual(report.stopped, "unmatched")
        self.assertEqual(report.unmatched, 1)
        self.assertEqual(report.journalled, 0)
        self.assertIsNone(store.mailbox(self.conn, "personal").cursor)
        with self.assertRaises(store.MessageNotFound):
            store.message_row(self.conn, "personal", "m1")

    def test_a_classifier_covers_the_residue_in_one_batch(self) -> None:
        self.add_rule()
        adapter = FakeMailbox(
            [
                message("m1"),
                message("m2", domain="other.example"),
                message("m3", domain="other.example"),
            ]
        )
        seen: list[int] = []

        def classify(messages):
            seen.append(len(messages))
            return [Decision(verb=Verb.FILE, target="Receipts") for _ in messages]

        report = self.sweep(adapter, classifier=classify)

        self.assertEqual(seen, [2])
        self.assertEqual(report.applied, 3)
        self.assertEqual(report.stopped, None)

    def test_a_classifier_of_the_wrong_length_is_refused(self) -> None:
        adapter = FakeMailbox([message("m1")])

        with self.assertRaises(SweepError):
            self.sweep(adapter, classifier=lambda messages: ())

        self.assertIsNone(store.mailbox(self.conn, "personal").cursor)
        self.assertEqual(store.take_lease(self.conn, "personal", "after", now=T0), 1)

    def test_a_dry_run_journals_and_advances_the_cursor(self) -> None:
        self.add_rule()
        store.set_dry_run(self.conn, "personal", True)
        adapter = FakeMailbox([message("m1")])

        report = self.sweep(adapter)

        self.assertEqual(report.dry_run, 1)
        self.assertEqual(report.applied, 0)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "dry_run")
        self.assertEqual(store.mailbox(self.conn, "personal").cursor, "1")
        self.assertEqual(adapter.applied, [])

    def test_dry_run_off_applies_the_open_dry_run_rows(self) -> None:
        self.add_rule()
        store.set_dry_run(self.conn, "personal", True)
        adapter = FakeMailbox([message("m1")])
        self.sweep(adapter)

        store.set_dry_run(self.conn, "personal", False)
        report = self.sweep(adapter, now=T0 + timedelta(minutes=10))

        self.assertEqual(report.applied, 1)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "confirmed")
        self.assertEqual(len(adapter.applied), 1)

    def test_a_paused_mailbox_lists_nothing(self) -> None:
        self.add_rule()
        store.set_paused(self.conn, "personal", True)
        adapter = FakeMailbox([message("m1")])

        report = self.sweep(adapter)

        self.assertEqual(report.stopped, "paused")
        self.assertEqual(report.listed, 0)
        self.assertEqual(adapter.applied, [])
        self.assertIsNone(store.mailbox(self.conn, "personal").cursor)
        status = self.conn.execute("SELECT status FROM mail_sweeps").fetchone()["status"]
        self.assertEqual(status, "ok")

    def test_a_second_sweep_is_busy_while_the_lease_is_held(self) -> None:
        store.take_lease(self.conn, "personal", "other", now=T0)

        with self.assertRaises(store.SweepBusy):
            self.sweep(FakeMailbox())

    def test_three_failed_attempts_become_a_decision(self) -> None:
        self.add_rule()
        adapter = FakeMailbox([message("m1")], fail_ids={"m1"})

        first = self.sweep(adapter)
        self.assertEqual(first.retryable, 1)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["attempts"], 1)

        second = self.sweep(adapter, now=T0 + timedelta(minutes=6))
        self.assertEqual(second.retryable, 1)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["attempts"], 2)

        third = self.sweep(adapter, now=T0 + timedelta(minutes=12))
        self.assertEqual(third.queued, 1)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "needs_decision")
        open_decisions = self.conn.execute(
            "SELECT COUNT(*) AS n FROM mail_decisions WHERE resolved_at IS NULL"
        ).fetchone()["n"]
        self.assertEqual(open_decisions, 1)

    def test_a_retryable_row_waits_for_its_backoff(self) -> None:
        self.add_rule()
        adapter = FakeMailbox([message("m1")], fail_ids={"m1"})
        self.sweep(adapter)

        report = self.sweep(adapter, now=T0 + timedelta(minutes=1))

        self.assertEqual(report.retryable, 1)
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["attempts"], 1)

    def test_three_consecutive_errors_stop_the_batch(self) -> None:
        self.add_rule()
        adapter = FakeMailbox(
            [message(f"m{index}") for index in range(4)],
            fail_ids={"m0", "m1", "m2", "m3"},
        )

        report = self.sweep(adapter)

        self.assertEqual(report.stopped, "consecutive_errors")
        self.assertEqual(report.errors, 3)
        self.assertEqual(store.message_row(self.conn, "personal", "m3")["state"], "pending")

    def test_an_open_ask_is_not_duplicated(self) -> None:
        store.ensure_mailbox(self.conn, "work", "google", behaviours=WORK_DEFAULTS, dry_run=False)
        adapter = FakeMailbox([message("w1", mailbox="work")])

        def classify(messages):
            return [Decision(verb=Verb.MARK_READ) for _ in messages]

        first = self.sweep(adapter, classifier=classify, mailbox="work")
        self.assertEqual(first.queued, 1)

        second = self.sweep(adapter, now=T0 + timedelta(minutes=10), classifier=classify, mailbox="work")
        self.assertEqual(second.queued, 0)
        self.assertEqual(second.waiting, 1)
        open_decisions = self.conn.execute(
            "SELECT COUNT(*) AS n FROM mail_decisions WHERE mailbox='work' AND resolved_at IS NULL"
        ).fetchone()["n"]
        self.assertEqual(open_decisions, 1)


class FakeMailboxTest(unittest.TestCase):
    def test_ensure_folder_is_idempotent(self) -> None:
        adapter = FakeMailbox(folders=[Folder(name="Receipts", folder_id="label-1")])

        self.assertEqual(adapter.ensure_folder("Receipts").folder_id, "label-1")
        created = adapter.ensure_folder("Finance")
        self.assertEqual(len(adapter.folders()), 2)
        self.assertEqual(adapter.ensure_folder("Finance"), created)

    def test_resync_returns_a_page_and_a_stable_lookup(self) -> None:
        adapter = FakeMailbox([message("m1"), message("m2")])

        page = adapter.resync(Scope.INBOX, 1)

        self.assertEqual(len(page.messages), 1)
        self.assertEqual(page.next_cursor, "1")
        self.assertFalse(page.complete)
        found = adapter.find_by_stable_id("stable-m2", [])
        self.assertIsNotNone(found)
        self.assertEqual(found.message_id, "m2")
        self.assertIsNone(adapter.find_by_stable_id("missing", ["Receipts"]))

    def test_content_hash_is_stable_and_content_sensitive(self) -> None:
        first = message("m1", subject="Your receipt")
        copy = message("m2", subject="Your receipt")
        changed = message("m3", subject="Your receipt", body="different")

        self.assertEqual(content_hash(first), content_hash(copy))
        self.assertNotEqual(content_hash(first), content_hash(changed))


class TaxonomyGateTest(unittest.TestCase):
    """Filing waits for an operator-approved taxonomy."""

    def setUp(self) -> None:
        self.conn = store.connect(":memory:")
        store.initialize(self.conn)
        store.ensure_mailbox(self.conn, "personal", "google", behaviours=PERSONAL_DEFAULTS, dry_run=False)
        store.insert_rule(
            self.conn,
            rule_id="op",
            account="personal",
            matcher={"domain": "github.com"},
            target="Receipts",
            source="operator",
            rank=40,
            priority=1,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_a_sweep_without_an_approved_taxonomy_files_nothing(self) -> None:
        adapter = FakeMailbox([message("m1")])

        report = sweep(self.conn, adapter, "personal", now=T0)

        self.assertEqual(report.listed, 1)
        self.assertEqual(report.taxonomy_missing, 1)
        self.assertEqual(report.applied, 0)
        self.assertEqual(adapter.applied, [])
        self.assertIn("taxonomy missing", report.summary())
        self.assertEqual(store.message_row(self.conn, "personal", "m1")["state"], "needs_decision")

    def test_a_proposed_taxonomy_still_does_not_unlock_filing(self) -> None:
        store.write_taxonomy(self.conn, "personal", [{"name": "Receipts", "risk": "low"}], state="proposed")
        adapter = FakeMailbox([message("m1")])

        report = sweep(self.conn, adapter, "personal", now=T0)

        self.assertEqual(report.taxonomy_missing, 1)
        self.assertEqual(adapter.applied, [])


if __name__ == "__main__":
    unittest.main()
