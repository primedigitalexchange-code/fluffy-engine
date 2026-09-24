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

    def test_store_supports_legacy_and_account_id_access_patterns(self) -> None:
        by_user = self.store.get_account("user-123", "123456789012")
        by_id = self.store.get_account(self.account.account_id)
        self.assertEqual(by_user.account_id, self.account.account_id)
        self.assertEqual(by_id.account_id, self.account.account_id)

        listed = self.store.list_accounts_for_user("user-123")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].account_id, self.account.account_id)

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

    def test_transaction_balance_and_history_helpers_work_through_wrapper(self) -> None:
        transaction = self.store.add_transaction(
            "user-123",
            "123456789012",
            amount="10.00",
            transaction_type="deposit",
            description="payroll",
        )
        self.assertEqual(transaction.resulting_balance, self.account.balance + transaction.amount)

        balance_by_user = self.store.get_balance_status("user-123", "123456789012")
        balance_by_id = self.store.get_balance_status(self.account.account_id)
        self.assertEqual(balance_by_user["balance"], "160.25")
        self.assertEqual(balance_by_id["balance"], "160.25")

        txns_by_user = self.store.list_transactions("user-123", "123456789012")
        txns_by_id = self.store.list_transactions(self.account.account_id)
        self.assertEqual(len(txns_by_user), 1)
        self.assertEqual(len(txns_by_id), 1)
        self.assertEqual(txns_by_user[0].transaction_id, txns_by_id[0].transaction_id)


if __name__ == "__main__":
    unittest.main()
