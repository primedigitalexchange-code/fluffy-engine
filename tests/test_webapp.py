from __future__ import annotations

from datetime import date
from decimal import Decimal
import os
import unittest

from fastapi.testclient import TestClient

from bank_account import BankAccountStore
from banking_connector import BankTransaction, ConnectedBankSession, LiveBankAccountData
from webapp.app import create_app


class FakePlaidConnector:
    def __init__(self) -> None:
        self.link_token_calls: list[str] = []
        self.exchange_calls: list[tuple[str, str]] = []
        self.transaction_calls: list[tuple[str, str | None]] = []

    def create_link_token(self, *, user_id: str) -> dict[str, str]:
        self.link_token_calls.append(user_id)
        return {
            "link_token": "link-sandbox-token",
            "expiration": "2099-01-01T00:00:00Z",
            "request_id": "req-link",
        }

    def connect_account(self, *, user_id: str, public_token: str) -> ConnectedBankSession:
        self.exchange_calls.append((user_id, public_token))
        return ConnectedBankSession(
            item_id="item-1",
            access_token_reference="item-1",
            accounts=(
                LiveBankAccountData(
                    account_id="plaid-account-1",
                    item_id="item-1",
                    account_number="000123456789",
                    routing_number="990000000",
                    balance=Decimal("2485.77"),
                    account_type="checking",
                    bank_name="Live Build Bank",
                ),
            ),
        )

    def get_transaction_history(
        self,
        item_id: str,
        *,
        start_date: date | str,
        end_date: date | str,
        account_id: str | None = None,
    ) -> list[BankTransaction]:
        self.transaction_calls.append((item_id, account_id))
        return [
            BankTransaction(
                transaction_id="txn-1",
                account_id=account_id or "plaid-account-1",
                amount=Decimal("12.34"),
                name="Coffee Shop",
                posted_on=date(2026, 9, 15),
                pending=False,
                iso_currency_code="USD",
                merchant_name="Coffee Shop",
            )
        ]


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["APP_USERNAME"] = "alice"
        os.environ["APP_PASSWORD"] = "alice-secret"
        os.environ["APP_JWT_SECRET"] = "unit-test-jwt-secret"
        os.environ["APP_COOKIE_SECURE"] = "true"
        self.store = BankAccountStore()
        self.connector = FakePlaidConnector()
        app = create_app(account_store=self.store, connector_factory=lambda: self.connector)
        self.client = TestClient(app)

    def test_login_success_sets_cookie_and_returns_token(self) -> None:
        response = self.client.post("/auth/login", json={"username": "alice", "password": "alice-secret"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["username"], "alice")
        self.assertTrue(payload["access_token"])
        set_cookie = response.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("Secure", set_cookie)

    def test_login_invalid_credentials_returns_401(self) -> None:
        response = self.client.post("/auth/login", json={"username": "alice", "password": "wrong"})
        self.assertEqual(response.status_code, 401)

    def test_protected_routes_require_authentication(self) -> None:
        response = self.client.get("/bank/accounts")
        self.assertEqual(response.status_code, 401)

    def test_bank_endpoints_work_with_mocked_connector(self) -> None:
        login = self.client.post("/auth/login", json={"username": "alice", "password": "alice-secret"}).json()
        headers = {"Authorization": "Bearer " + login["access_token"]}

        link = self.client.post("/bank/link-token", headers=headers)
        self.assertEqual(link.status_code, 200)
        self.assertEqual(link.json()["link_token"], "link-sandbox-token")

        exchange = self.client.post("/bank/exchange-token", json={"public_token": "public-sandbox-good"}, headers=headers)
        self.assertEqual(exchange.status_code, 200)
        self.assertEqual(exchange.json()["accounts"][0]["masked_account_number"], "********6789")

        accounts = self.client.get("/bank/accounts", headers=headers)
        self.assertEqual(accounts.status_code, 200)
        account = accounts.json()["accounts"][0]
        self.assertEqual(account["account_id"], "plaid-account-1")
        self.assertEqual(account["masked_account_number"], "********6789")
        self.assertEqual(account["balance"], "2485.77")

        transactions = self.client.get(f"/bank/accounts/{account['account_id']}/transactions", headers=headers)
        self.assertEqual(transactions.status_code, 200)
        self.assertEqual(transactions.json()["transactions"][0]["name"], "Coffee Shop")
        self.assertEqual(self.connector.transaction_calls[-1], ("item-1", "plaid-account-1"))

    def test_cross_user_account_access_returns_404(self) -> None:
        self.store.create_account(
            user_id="bob",
            account_number="000000001111",
            account_type="checking",
            balance="10.00",
            data_source="live",
            routing_number="990000000",
            provider="plaid",
            provider_item_id="item-bob",
            provider_account_id="plaid-bob-1",
            access_token_reference="item-bob",
        )
        login = self.client.post("/auth/login", json={"username": "alice", "password": "alice-secret"}).json()
        headers = {"Authorization": "Bearer " + login["access_token"]}

        response = self.client.get("/bank/accounts/plaid-bob-1/transactions", headers=headers)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
