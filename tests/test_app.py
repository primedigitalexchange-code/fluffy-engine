from datetime import date
from decimal import Decimal
import unittest

from fastapi.testclient import TestClient

from app import create_app
from banking_connector import (
    AppCredentials,
    BankTransaction,
    ConnectedBankSession,
    LiveBankAccountData,
)


class FakeConnector:
    def __init__(self) -> None:
        self.last_transaction_request = None

    def create_link_token(self, *, user_id: str) -> dict[str, str]:
        return {"link_token": "link-test", "expiration": "2026-10-01T12:00:00Z"}

    def connect_account(self, *, user_id: str, public_token: str) -> ConnectedBankSession:
        return ConnectedBankSession(
            item_id="item-1",
            access_token_reference="item-1",
            accounts=(
                LiveBankAccountData(
                    account_id="account-1",
                    item_id="item-1",
                    account_number="000123456789",
                    routing_number="990000000",
                    balance=Decimal("42.50"),
                    account_type="checking",
                ),
            ),
        )

    def get_transaction_history(
        self,
        item_id: str,
        *,
        start_date: date,
        end_date: date,
        account_id: str | None = None,
    ) -> list[BankTransaction]:
        self.last_transaction_request = (item_id, start_date, end_date, account_id)
        return [
            BankTransaction(
                transaction_id="txn-1",
                account_id="account-1",
                amount=Decimal("12.34"),
                name="Coffee Shop",
                posted_on=date(2026, 9, 15),
                pending=False,
                iso_currency_code="USD",
            )
        ]


class BankingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connector = FakeConnector()
        self.client = TestClient(
            create_app(
                connector=self.connector,
                credentials=AppCredentials("api-user", "api-password"),
            )
        )
        self.auth = ("api-user", "api-password")

    def test_health_is_public_and_authentication_is_required_for_accounts(self) -> None:
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        response = self.client.get("/accounts")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Basic")

    def test_non_ascii_application_password_is_rejected_cleanly(self) -> None:
        client = TestClient(
            create_app(credentials=AppCredentials("api-user", "pässword"))
        )
        response = client.get("/accounts", auth=("api-user", "pässword"))
        self.assertEqual(response.status_code, 401)

    def test_mock_account_create_list_balance_and_transactions(self) -> None:
        response = self.client.post(
            "/accounts",
            auth=self.auth,
            json={
                "account_number": "mock-123",
                "account_type": "checking",
                "balance": "25.00",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["account"]["data_source"], "mock")

        deposit = self.client.post(
            "/accounts/mock-123/transactions",
            auth=self.auth,
            json={"amount": "5.00", "transaction_type": "deposit"},
        )
        self.assertEqual(deposit.status_code, 201)
        self.assertEqual(deposit.json()["transaction"]["amount"], "5.00")

        listed = self.client.get("/accounts", auth=self.auth)
        self.assertEqual(len(listed.json()["accounts"]), 1)
        balance = self.client.get("/accounts/mock-123/balance", auth=self.auth)
        self.assertEqual(balance.json()["balance"], "30.00")
        transactions = self.client.get("/accounts/mock-123/transactions", auth=self.auth)
        self.assertEqual(transactions.status_code, 200)
        self.assertEqual(len(transactions.json()["transactions"]), 1)

    def test_create_account_validation_and_duplicates(self) -> None:
        payload = {"account_number": "mock-123", "account_type": "checking", "balance": "1.00"}
        self.assertEqual(self.client.post("/accounts", auth=self.auth, json=payload).status_code, 201)
        self.assertEqual(self.client.post("/accounts", auth=self.auth, json=payload).status_code, 409)

        invalid = {**payload, "account_number": "mock-456", "balance": "1.001"}
        self.assertEqual(self.client.post("/accounts", auth=self.auth, json=invalid).status_code, 422)

    def test_mock_transfer_and_live_transfer_rejection(self) -> None:
        for account_number, balance in (("source", "20.00"), ("destination", "5.00")):
            response = self.client.post(
                "/accounts",
                auth=self.auth,
                json={
                    "account_number": account_number,
                    "account_type": "checking",
                    "balance": balance,
                },
            )
            self.assertEqual(response.status_code, 201)

        response = self.client.post(
            "/transfers",
            auth=self.auth,
            json={
                "source_account_number": "source",
                "destination_account_number": "destination",
                "amount": "7.50",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["transfer"]["amount"], "7.50")
        self.assertEqual(
            self.client.get("/accounts/source/balance", auth=self.auth).json()["balance"],
            "12.50",
        )
        self.assertEqual(
            self.client.get("/accounts/destination/balance", auth=self.auth).json()["balance"],
            "12.50",
        )
        source_history = self.client.get("/accounts/source/transactions", auth=self.auth).json()
        destination_history = self.client.get(
            "/accounts/destination/transactions", auth=self.auth
        ).json()
        self.assertEqual(source_history["transactions"][0]["transaction_type"], "transfer_out")
        self.assertEqual(destination_history["transactions"][0]["transaction_type"], "transfer_in")
        self.assertEqual(
            source_history["transactions"][0]["transfer_id"],
            destination_history["transactions"][0]["transfer_id"],
        )

        self.client.post(
            "/banking/accounts",
            auth=self.auth,
            json={"public_token": "public-test"},
        )
        live_transfer = self.client.post(
            "/transfers",
            auth=self.auth,
            json={
                "source_account_number": "source",
                "destination_account_number": "000123456789",
                "amount": "1.00",
            },
        )
        self.assertEqual(live_transfer.status_code, 409)

    def test_linking_and_live_transaction_history(self) -> None:
        link_token = self.client.post("/banking/link-token", auth=self.auth)
        self.assertEqual(link_token.status_code, 200)
        self.assertEqual(link_token.json()["link_token"], "link-test")

        linked = self.client.post(
            "/banking/accounts",
            auth=self.auth,
            json={"public_token": "public-test"},
        )
        self.assertEqual(linked.status_code, 201)
        self.assertEqual(linked.json()["accounts"][0]["data_source"], "live")

        rejected_transaction = self.client.post(
            "/accounts/000123456789/transactions",
            auth=self.auth,
            json={"amount": "1.00", "transaction_type": "deposit"},
        )
        self.assertEqual(rejected_transaction.status_code, 409)

        missing_dates = self.client.get(
            "/accounts/000123456789/transactions",
            auth=self.auth,
        )
        self.assertEqual(missing_dates.status_code, 422)

        invalid_date_range = self.client.get(
            "/accounts/000123456789/transactions",
            auth=self.auth,
            params={"start_date": "2026-09-30", "end_date": "2026-09-01"},
        )
        self.assertEqual(invalid_date_range.status_code, 422)

        history = self.client.get(
            "/accounts/000123456789/transactions",
            auth=self.auth,
            params={"start_date": "2026-09-01", "end_date": "2026-09-30"},
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["transactions"][0]["amount"], "12.34")
        self.assertEqual(
            self.connector.last_transaction_request,
            ("item-1", date(2026, 9, 1), date(2026, 9, 30), "account-1"),
        )

    def test_account_routes_do_not_disclose_another_users_records(self) -> None:
        self.client.post(
            "/accounts",
            auth=self.auth,
            json={"account_number": "private-1", "account_type": "checking"},
        )
        other_user_client = TestClient(
            create_app(
                credentials=AppCredentials("other-user", "other-password"),
            )
        )
        response = other_user_client.get(
            "/accounts/private-1",
            auth=("other-user", "other-password"),
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
