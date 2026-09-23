import unittest

from bank_accounts import AccountStatus, BankAccountStore, ValidationError, handle_request


class BankAccountsCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BankAccountStore()
        self.account = self.store.create_account(
            user_id="user-123",
            account_number="123456789012",
            account_type="checking",
            balance="150.25",
            currency="usd",
        )

    def test_compatibility_package_exposes_masked_http_responses(self) -> None:
        status_code, payload = handle_request(
            self.store,
            method="GET",
            path=f"/accounts/{self.account.account_id}",
            user_id="user-123",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["account"]["masked_account_number"], "********9012")
        self.assertEqual(payload["account"]["currency"], "USD")
        self.assertNotIn("account_number", payload["account"])

    def test_status_updates_require_reason_when_reactivating_suspended_accounts(self) -> None:
        self.store.update_account(self.account.account_id, status=AccountStatus.SUSPENDED)

        with self.assertRaises(ValidationError):
            self.store.update_account(self.account.account_id, status=AccountStatus.ACTIVE)

        updated = self.store.update_account(
            self.account.account_id,
            status=AccountStatus.ACTIVE,
            status_reason="manual review passed",
        )
        self.assertEqual(updated.status, AccountStatus.ACTIVE)
        self.assertEqual(len(self.store.get_status_history(self.account.account_id)), 2)


if __name__ == "__main__":
    unittest.main()
