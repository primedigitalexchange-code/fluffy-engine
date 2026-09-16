from decimal import Decimal
import unittest

from bank_account.module import (
    AccountStatus,
    BankAccount,
    BankAccountStore,
    NotFoundError,
    ValidationError,
    get_account_balance_status,
    list_user_accounts,
    retrieve_account,
    update_account,
)


class BankAccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BankAccountStore()
        self.primary = BankAccount(
            user_id="user-1",
            account_number="123456789012",
            account_type="checking",
            balance=Decimal("250.00"),
        )
        self.secondary = BankAccount(
            user_id="user-1",
            account_number="123456789013",
            account_type="savings",
            balance=Decimal("950.25"),
            status=AccountStatus.INACTIVE,
        )
        self.other_user = BankAccount(
            user_id="user-2",
            account_number="123456789014",
            account_type="checking",
            balance=Decimal("1.00"),
        )

        self.store.create_account(self.primary)
        self.store.create_account(self.secondary)
        self.store.create_account(self.other_user)

    def test_supports_multiple_accounts_per_user(self) -> None:
        accounts = self.store.list_accounts("user-1")
        self.assertEqual(len(accounts), 2)

    def test_retrieves_account_and_tracks_status(self) -> None:
        account = self.store.get_account("user-1", "123456789013")
        self.assertEqual(account.status, AccountStatus.INACTIVE)

    def test_security_check_hides_cross_user_account_access(self) -> None:
        with self.assertRaises(NotFoundError):
            self.store.get_account("user-1", "123456789014")

    def test_update_account_details_and_status(self) -> None:
        updated = self.store.update_account(
            "user-1",
            "123456789012",
            account_type="business",
            status="suspended",
        )
        self.assertEqual(updated.account_type, "business")
        self.assertEqual(updated.status, AccountStatus.SUSPENDED)

    def test_add_transaction_updates_balance(self) -> None:
        self.store.add_transaction(
            "user-1",
            "123456789012",
            amount="50.00",
            transaction_type="credit",
        )
        balance = self.store.get_balance_status("user-1", "123456789012")
        self.assertEqual(balance, {"balance": "300.00", "status": "active"})
        transactions = self.store.list_transactions("user-1", "123456789012")
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].transaction_type, "credit")
        self.assertEqual(transactions[0].amount, Decimal("50.00"))

    def test_suspended_account_rejects_transactions(self) -> None:
        self.store.update_account("user-1", "123456789012", status="suspended")
        with self.assertRaises(ValidationError):
            self.store.add_transaction(
                "user-1",
                "123456789012",
                amount="10.00",
                transaction_type="credit",
            )

    def test_inactive_account_rejects_transactions(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.add_transaction(
                "user-1",
                "123456789013",
                amount="10.00",
                transaction_type="credit",
            )

    def test_debit_cannot_overdraw_account(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.add_transaction(
                "user-1",
                "123456789012",
                amount="999.00",
                transaction_type="debit",
            )

    def test_transaction_amount_requires_two_decimal_places(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.add_transaction(
                "user-1",
                "123456789012",
                amount="10.999",
                transaction_type="credit",
            )

    def test_invalid_account_data_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_account(
                BankAccount(
                    user_id="user-1",
                    account_number="abc",
                    account_type="checking",
                    balance=Decimal("10.00"),
                )
            )

    def test_rejects_duplicate_account_number_across_users(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_account(
                BankAccount(
                    user_id="user-3",
                    account_number="123456789012",
                    account_type="checking",
                    balance=Decimal("10.00"),
                )
            )

    def test_list_accounts_rejects_blank_user_id(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.list_accounts("   ")


class BankAccountEndpointTests(unittest.TestCase):
    def test_api_style_endpoints(self) -> None:
        store = BankAccountStore()
        store.create_account(
            BankAccount(
                user_id="user-3",
                account_number="123456789015",
                account_type="checking",
                balance=Decimal("99.00"),
            )
        )

        account = retrieve_account(store, "user-3", "123456789015")
        self.assertEqual(account["account_number"], "123456789015")
        self.assertEqual(account["balance"], "99.00")
        self.assertEqual(account["status"], "active")

        listing = list_user_accounts(store, "user-3")
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["balance"], "99.00")
        self.assertEqual(listing[0]["status"], "active")

        updated = update_account(
            store,
            "user-3",
            "123456789015",
            {"status": "inactive", "account_type": "savings"},
        )
        self.assertEqual(updated["status"], "inactive")

        balance_status = get_account_balance_status(store, "user-3", "123456789015")
        self.assertEqual(balance_status, {"balance": "99.00", "status": "inactive"})

    def test_update_endpoint_rejects_invalid_status(self) -> None:
        store = BankAccountStore()
        store.create_account(
            BankAccount(
                user_id="user-3",
                account_number="123456789016",
                account_type="checking",
                balance=Decimal("10.00"),
            )
        )

        with self.assertRaises(ValidationError):
            update_account(
                store,
                "user-3",
                "123456789016",
                {"status": "paused"},
            )


if __name__ == "__main__":
    unittest.main()
