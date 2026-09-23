from decimal import Decimal
import unittest

from bank_accounts import AccountStatus, BankAccountStore, ValidationError, handle_request


class BankAccountsModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BankAccountStore()
        self.account = self.store.create_account(
            user_id="user-123",
            account_number="123456789012",
            account_type="checking",
            balance="150.25",
            currency="usd",
        )

    def test_create_account_tracks_required_fields_and_masks_number(self) -> None:
        self.assertEqual(self.account.status, AccountStatus.ACTIVE)
        self.assertEqual(self.account.currency, "USD")
        self.assertEqual(self.account.masked_account_number, "********9012")
        self.assertEqual(self.account.created_at, self.account.updated_at)

    def test_invalid_creation_inputs_raise_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_account(
                user_id="user-123",
                account_number="abc123",
                account_type="checking",
            )

        with self.assertRaises(ValidationError):
            self.store.create_account(
                user_id="user-123",
                account_number="999999",
                account_type="checking",
                balance="-1.00",
            )

        with self.assertRaises(ValidationError):
            self.store.create_account(
                user_id="user-123",
                account_number="888888",
                account_type="checking",
                currency="US",
            )

        with self.assertRaises(ValidationError):
            self.store.create_account(
                user_id="user-456",
                account_number="123456789012",
                account_type="checking",
            )

    def test_patch_requires_reason_for_suspended_to_active_transition(self) -> None:
        self.store.update_account(self.account.account_id, status="inactive")
        history = self.store.get_status_history(self.account.account_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].old_status, AccountStatus.ACTIVE)
        self.assertEqual(history[0].new_status, AccountStatus.INACTIVE)

        self.store.update_account(self.account.account_id, status="suspended")

        status_code, payload = handle_request(
            self.store,
            method="PATCH",
            path=f"/accounts/{self.account.account_id}",
            user_id="user-123",
            payload={"status": "active"},
        )

        self.assertEqual(status_code, 400)
        self.assertIn("status_reason", payload["error"])

        status_code, payload = handle_request(
            self.store,
            method="PATCH",
            path=f"/accounts/{self.account.account_id}",
            user_id="user-123",
            payload={"status": "active", "status_reason": "manual review passed"},
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(payload["account"]["status"], "active")
        history = self.store.get_status_history(self.account.account_id)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1].old_status, AccountStatus.SUSPENDED)
        self.assertEqual(history[-1].new_status, AccountStatus.ACTIVE)
        self.assertEqual(history[-1].reason, "manual review passed")

    def test_endpoints_enforce_ownership_and_masked_account_numbers(self) -> None:
        status_code, payload = handle_request(
            self.store,
            method="GET",
            path=f"/accounts/{self.account.account_id}",
            user_id="different-user",
        )
        self.assertEqual(status_code, 403)
        self.assertIn("forbidden", payload["error"])

        status_code, payload = handle_request(
            self.store,
            method="GET",
            path=f"/accounts/{self.account.account_id}",
            user_id="user-123",
        )
        self.assertEqual(status_code, 200)
        account_payload = payload["account"]
        self.assertEqual(account_payload["masked_account_number"], "********9012")
        self.assertNotIn("account_number", account_payload)

        status_code, payload = handle_request(
            self.store,
            method="GET",
            path="/users/user-123/accounts",
            user_id=" user-123 ",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(len(payload["accounts"]), 1)

        status_code, payload = handle_request(
            self.store,
            method="GET",
            path="/users/ /accounts",
            user_id="user-123",
        )
        self.assertEqual(status_code, 400)
        self.assertIn("user_id", payload["error"])

        status_code, payload = handle_request(
            self.store,
            method="GET",
            path="/users//accounts",
            user_id="user-123",
        )
        self.assertEqual(status_code, 400)
        self.assertIn("user_id", payload["error"])

    def test_user_account_list_and_balance_endpoint(self) -> None:
        other_account = self.store.create_account(
            user_id="user-123",
            account_number="111122223333",
            account_type="savings",
            balance=Decimal("99.00"),
        )

        status_code, payload = handle_request(
            self.store,
            method="GET",
            path="/users/user-123/accounts",
            user_id="user-123",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(len(payload["accounts"]), 2)
        self.assertEqual({item["account_id"] for item in payload["accounts"]}, {self.account.account_id, other_account.account_id})

        status_code, payload = handle_request(
            self.store,
            method="GET",
            path=f"/accounts/{self.account.account_id}/balance",
            user_id="user-123",
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(payload["balance"], "150.25")
        self.assertEqual(payload["status"], "active")


if __name__ == "__main__":
    unittest.main()
