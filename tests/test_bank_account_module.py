from decimal import Decimal
import unittest

from bank_account import (
    AccountStatus,
    BankAccountStore,
    DataSource,
    ValidationError,
    create_account_from_live_api,
    create_account_from_mock_data,
    list_user_accounts,
    retrieve_account,
    update_account,
)
from bank_profile import build_bank_profile


class LiveAccountRecord:
    account_id = "account-123"
    item_id = "item-123"
    account_number = "000123456789"
    routing_number = "990000000"
    balance = Decimal("2485.77")
    account_type = "checking"
    bank_name = "Live Build Bank"
    provider = "plaid"


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
            state="il",
            postal_code="62704",
        )

        self.assertEqual(account.routing_number, "990000000")
        self.assertEqual(account.bank_name, "Live Build Bank")
        self.assertEqual(account.bank_address, "742 Evergreen Terrace, Springfield, IL 62704, USA")
        self.assertEqual(account.city, "Springfield")
        self.assertEqual(account.state, "IL")
        self.assertEqual(account.postal_code, "62704")
        self.assertEqual(account.data_source, DataSource.MOCK)

        payload = retrieve_account(self.store, "user-123", "000123456789")
        self.assertEqual(payload["account"]["balance"], "2485.77")
        self.assertEqual(payload["account"]["status"], "active")
        self.assertEqual(payload["account"]["data_source"], "mock")
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
        self.assertEqual(account.data_source, DataSource.MOCK)
        self.assertEqual(list_user_accounts(self.store, "user-123")["accounts"][0]["account_number"], "000123456780")

    def test_create_account_from_mock_data_factory_sets_mock_source(self) -> None:
        account = create_account_from_mock_data(
            self.store,
            user_id="user-456",
            profile=build_bank_profile(account_name="Payroll"),
            account_type="checking",
        )

        self.assertEqual(account.data_source, DataSource.MOCK)
        self.assertEqual(account.bank_name, "Live Build Bank")

    def test_create_account_from_live_api_factory_sets_live_source(self) -> None:
        account = create_account_from_live_api(
            self.store,
            user_id="user-789",
            live_account=LiveAccountRecord(),
            access_token_reference="item-123",
        )

        self.assertEqual(account.data_source, DataSource.LIVE)
        self.assertEqual(account.provider, "plaid")
        self.assertEqual(account.provider_item_id, "item-123")
        self.assertEqual(account.provider_account_id, "account-123")

    def test_live_accounts_require_access_token_reference(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_account(
                user_id="user-789",
                account_number="000123456799",
                account_type="checking",
                balance="1.00",
                data_source="live",
                provider="plaid",
            )

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
        self.assertEqual(payload["account"]["state"], "IL")

    def test_invalid_bank_details_raise_validation_error(self) -> None:
        invalid_cases = [
            {"routing_number": "1234"},
            {"bank_name": "   "},
            {"bank_address": ""},
            {"city": " "},
            {"state": "Illinois"},
            {"postal_code": "6270A"},
        ]

        for index, updates in enumerate(invalid_cases, start=1):
            with self.subTest(updates=updates):
                with self.assertRaises(ValidationError):
                    self.store.create_account(
                        user_id="user-123",
                        account_number=f"invalid-account-{index}",
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

    def test_inactive_account_cannot_accept_transactions(self) -> None:
        self.store.create_account(
            user_id="user-123",
            account_number="000123456784",
            account_type="checking",
            status=AccountStatus.INACTIVE,
            balance="10.00",
        )

        with self.assertRaises(ValidationError):
            self.store.add_transaction("user-123", "000123456784", "1.00", "deposit")

    def test_float_money_values_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_account(
                user_id="user-123",
                account_number="000123456785",
                account_type="checking",
                balance=10.25,
            )

        self.store.create_account(
            user_id="user-123",
            account_number="000123456786",
            account_type="checking",
            balance="10.00",
        )

        with self.assertRaises(ValidationError):
            self.store.add_transaction("user-123", "000123456786", 1.25, "deposit")

    def test_non_finite_money_values_are_rejected(self) -> None:
        self.store.create_account(
            user_id="user-123",
            account_number="000123456787",
            account_type="checking",
            balance="10.00",
        )

        with self.assertRaises(ValidationError):
            self.store.add_transaction("user-123", "000123456787", "NaN", "deposit")

    def test_money_values_with_more_than_two_decimal_places_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_account(
                user_id="user-123",
                account_number="000123456788",
                account_type="checking",
                balance="10.999",
            )

        self.store.create_account(
            user_id="user-123",
            account_number="000123456789",
            account_type="checking",
            balance="10.00",
        )

        with self.assertRaises(ValidationError):
            self.store.add_transaction("user-123", "000123456789", "1.999", "deposit")


if __name__ == "__main__":
    unittest.main()
