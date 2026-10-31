"""Offline ownership checks for the disposable Orders review launcher."""

import unittest
from pathlib import Path
from unittest.mock import patch

import orders_ux_review as review


class ReviewSafetyTests(unittest.TestCase):
    def test_existing_state_fails_before_docker_resource_creation(self):
        with patch.object(Path, "exists", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Review state already exists"):
                review.check_environment()

    def test_untracked_similarly_named_container_fails_closed(self):
        with patch.object(Path, "exists", return_value=False), patch.object(
            review, "command", return_value="mbb-p2-postgres\nmbb-orders-ux-postgres-unknown"
        ):
            with self.assertRaisesRegex(RuntimeError, "Untracked Orders UX"):
                review.check_environment()

    def test_container_identity_and_label_must_both_match(self):
        with patch.object(review, "command", return_value="different-id right-token"):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                review.owned_container("mbb-orders-ux-postgres-token", "expected-id", "right-token")
        with patch.object(review, "command", return_value="expected-id wrong-token"):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                review.owned_container("mbb-orders-ux-postgres-token", "expected-id", "right-token")

    def test_stop_checks_all_owners_before_removing_anything(self):
        state = {"token": "token", "runtime_dir": str(review.STATE.parent / "token"),
                 "containers": {"owned-one": "id-one", "foreign-two": "id-two"}}
        with patch.object(review, "owned_container", side_effect=[True, RuntimeError("ownership mismatch")]), patch.object(
            review.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                review.stop_owned(state)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
