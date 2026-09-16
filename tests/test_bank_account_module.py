import unittest

from bank_account import (
    AccountStatus,
    BankAccountStore,
    ValidationError,
    list_user_accounts,
    retrieve_account,
    update_account,
)


class BankAccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BankAccountStore()

    def test_create_account_supports_full_bank_details(self) -> None:
        account = self.store.create_account(
            user_id="user-123",
            account_number="000123456789",
            account_type="checking",
            balance="2485.77",
            routing_number="990000000",
            bank_name="Live Build Bank",
            bank_address="742 Evergreen Terrace, Springfield, IL 62704, USA",
            city="Springfield",
            state="IL",
            postal_code="62704",
        )

        self.assertEqual(account.routing_number, "990000000")
        self.assertEqual(account.bank_name, "Live Build Bank")
        self.assertEqual(account.bank_address, "742 Evergreen Terrace, Springfield, IL 62704, USA")
        self.assertEqual(account.city, "Springfield")
        self.assertEqual(account.state, "IL")
        self.assertEqual(account.postal_code, "62704")

        payload = retrieve_account(self.store, "user-123", "000123456789")
        self.assertEqual(payload["account"]["balance"], "2485.77")
        self.assertEqual(payload["account"]["status"], "active")
        self.assertEqual(payload["account"]["routing_number"], "990000000")
        self.assertEqual(
            list_user_accounts(self.store, "user-123")["accounts"][0]["bank_address"],
            "742 Evergreen Terrace, Springfield, IL 62704, USA",
        )

    def test_create_account_keeps_bank_details_optional(self) -> None:
        account = self.store.create_account(
            user_id="user-123",
            account_number="000123456780",
            account_type="savings",
        )

        self.assertIsNone(account.routing_number)
        self.assertIsNone(account.bank_name)
        self.assertEqual(list_user_accounts(self.store, "user-123")["accounts"][0]["account_number"], "000123456780")

    def test_list_accounts_is_sorted_by_account_number(self) -> None:
        self.store.create_account(
            user_id="user-123",
            account_number="000123456790",
            account_type="checking",
        )
        self.store.create_account(
            user_id="user-123",
            account_number="000123456788",
            account_type="savings",
        )

        self.assertEqual(
            [account["account_number"] for account in list_user_accounts(self.store, "user-123")["accounts"]],
            ["000123456788", "000123456790"],
        )

    def test_update_account_supports_bank_details(self) -> None:
        self.store.create_account(
            user_id="user-123",
            account_number="000123456781",
            account_type="checking",
        )

        payload = update_account(
            self.store,
            "user-123",
            "000123456781",
            bank_name="Live Build Bank",
            bank_address="742 Evergreen Terrace, Springfield, IL 62704, USA",
            city="Springfield",
            state="IL",
            postal_code="62704-0001",
            routing_number="990000000",
            status=AccountStatus.SUSPENDED,
        )

        self.assertEqual(payload["account"]["bank_name"], "Live Build Bank")
        self.assertEqual(payload["account"]["postal_code"], "62704-0001")
        self.assertEqual(payload["account"]["status"], "suspended")

    def test_invalid_bank_details_raise_validation_error(self) -> None:
        invalid_cases = [
            {"routing_number": "1234"},
            {"bank_name": "   "},
            {"bank_address": ""},
            {"city": " "},
            {"state": "Illinois"},
            {"postal_code": "6270A"},
        ]

        for updates in invalid_cases:
            with self.subTest(updates=updates):
                with self.assertRaises(ValidationError):
                    self.store.create_account(
                        user_id="user-123",
                        account_number=f"acct-{len(updates)}-{next(iter(updates.items()))[1]}",
                        account_type="checking",
                        **updates,
                    )

    def test_transactions_and_balance_still_work_with_bank_details(self) -> None:
        self.store.create_account(
            user_id="user-123",
            account_number="000123456782",
            account_type="checking",
            balance="100.00",
            bank_name="Live Build Bank",
            bank_address="742 Evergreen Terrace, Springfield, IL 62704, USA",
            city="Springfield",
            state="IL",
        )

        self.store.add_transaction("user-123", "000123456782", "25.00", "deposit")
        self.store.add_transaction("user-123", "000123456782", "10.00", "withdrawal")

        balance_status = self.store.get_balance_status("user-123", "000123456782")
        self.assertEqual(balance_status["balance"], "115.00")

    def test_withdrawal_cannot_overdraw_account(self) -> None:
        self.store.create_account(
            user_id="user-123",
            account_number="000123456783",
            account_type="checking",
            balance="10.00",
        )

        with self.assertRaises(ValidationError):
            self.store.add_transaction("user-123", "000123456783", "15.00", "withdrawal")


if __name__ == "__main__":
    unittest.main()
