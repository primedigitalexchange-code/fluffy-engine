from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from fastapi.testclient import TestClient

from bank_account import BankAccountStore
from banking_connector import (
    BankingAuthenticationError,
    ConnectedBankSession,
    LiveBankAccountData,
)
from webapp.app import AUTH_COOKIE_NAME, create_app


class FakePlaidConnector:
    def __init__(self) -> None:
        self.link_token_calls: list[str] = []
        self.connected: list[tuple[str, str]] = []
        self.transaction_calls: list[dict[str, object]] = []

    def create_link_token(self, *, user_id: str) -> dict[str, str]:
        self.link_token_calls.append(user_id)
        return {
            "link_token": "link-sandbox-token",
            "expiration": "2026-09-30T00:00:00Z",
            "request_id": "request-1",
        }

    def connect_account(self, *, user_id: str, public_token: str) -> ConnectedBankSession:
        self.connected.append((user_id, public_token))
        account = LiveBankAccountData(
            account_id="provider-account-1",
            item_id="item-123",
            account_number="000123456789",
            routing_number="990000000",
            balance=Decimal("2485.77"),
            account_type="checking",
            bank_name="Live Build Bank",
        )
        return ConnectedBankSession(
            item_id="item-123",
            access_token_reference="item-123",
            accounts=(account,),
        )

    def get_transaction_history(self, item_id: str, *, start_date, end_date, account_id: str):
        self.transaction_calls.append(
            {
                "item_id": item_id,
                "start_date": start_date,
                "end_date": end_date,
                "account_id": account_id,
            }
        )
        return [
            type(
                "Txn",
                (),
                {
                    "transaction_id": "txn-1",
                    "amount": Decimal("12.34"),
                    "name": "Coffee Shop",
                    "posted_on": datetime(2026, 9, 22, tzinfo=UTC).date(),
                    "pending": False,
                    "iso_currency_code": "USD",
                    "merchant_name": "Coffee Shop",
                },
            )()
        ]


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BankAccountStore()
        self.connector = FakePlaidConnector()
        self.env = {
            "APP_USERNAME": "demo-user",
            "APP_PASSWORD": "demo-password",
            "APP_JWT_SECRET": "test-jwt-secret",
            "APP_CORS_ORIGINS": "http://localhost:3000,http://127.0.0.1:8000",
        }
        app = create_app(
            env=self.env,
            account_store=self.store,
            connector_factory=lambda: self.connector,
            clock=lambda: datetime.now(UTC),
        )
        self.client = TestClient(app)

    def login(self) -> dict[str, str]:
        response = self.client.post(
            "/auth/login",
            json={"username": "demo-user", "password": "demo-password"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_login_sets_cookie_and_returns_bearer_token(self) -> None:
        response = self.client.post(
            "/auth/login",
            json={"username": "demo-user", "password": "demo-password"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "demo-user")
        self.assertIn("access_token", response.json())
        self.assertIn(f"{AUTH_COOKIE_NAME}=", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_login_rejects_invalid_credentials(self) -> None:
        response = self.client.post(
            "/auth/login",
            json={"username": "demo-user", "password": "wrong-password"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid username or password")

    def test_auth_me_accepts_cookie_and_bearer_token(self) -> None:
        login_payload = self.login()

        cookie_response = self.client.get("/auth/me")
        bearer_client = TestClient(
            create_app(
                env=self.env,
                account_store=self.store,
                connector_factory=lambda: self.connector,
                clock=lambda: datetime.now(UTC),
            )
        )
        bearer_response = bearer_client.get(
            "/auth/me",
            headers={"Authorization": "Bearer " + login_payload["access_token"]},
        )

        self.assertEqual(cookie_response.status_code, 200)
        self.assertEqual(cookie_response.json(), {"username": "demo-user"})
        self.assertEqual(bearer_response.status_code, 200)
        self.assertEqual(bearer_response.json(), {"username": "demo-user"})

    def test_protected_routes_require_authentication(self) -> None:
        client = TestClient(
            create_app(
                env=self.env,
                account_store=self.store,
                connector_factory=lambda: self.connector,
            )
        )

        response = client.get("/bank/accounts")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Authentication required")

    def test_link_exchange_and_account_listing_only_return_current_user_accounts(self) -> None:
        self.login()
        self.store.create_account(
            user_id="other-user",
            account_number="000000000001",
            account_type="checking",
            balance="1.00",
        )

        link_response = self.client.post("/bank/link-token")
        exchange_response = self.client.post(
            "/bank/exchange-token",
            json={"public_token": "public-sandbox-token"},
        )
        accounts_response = self.client.get("/bank/accounts")

        self.assertEqual(link_response.status_code, 200)
        self.assertEqual(link_response.json()["link_token"], "link-sandbox-token")
        self.assertEqual(exchange_response.status_code, 200)
        self.assertEqual(self.connector.connected, [("demo-user", "public-sandbox-token")])
        self.assertEqual(self.connector.link_token_calls, ["demo-user"])
        accounts = accounts_response.json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["account_id"], "provider-account-1")
        self.assertEqual(accounts[0]["masked_account_number"], "****6789")
        self.assertEqual(accounts[0]["bank_name"], "Live Build Bank")

    def test_transactions_require_account_ownership_and_use_404_semantics(self) -> None:
        self.login()
        exchange_response = self.client.post(
            "/bank/exchange-token",
            json={"public_token": "public-sandbox-token"},
        )
        account_id = exchange_response.json()["accounts"][0]["account_id"]
        self.store.create_account(
            user_id="other-user",
            account_number="000123450000",
            account_type="checking",
            balance="2.00",
        )

        ok_response = self.client.get(f"/bank/accounts/{account_id}/transactions")
        missing_response = self.client.get("/bank/accounts/not-owned/transactions")

        self.assertEqual(ok_response.status_code, 200)
        self.assertEqual(ok_response.json()["source"], "plaid")
        self.assertEqual(ok_response.json()["transactions"][0]["name"], "Coffee Shop")
        self.assertEqual(missing_response.status_code, 404)
        self.assertEqual(missing_response.json()["detail"], "Account not found")
        self.assertEqual(self.connector.transaction_calls[0]["item_id"], "item-123")
        self.assertEqual(self.connector.transaction_calls[0]["account_id"], "provider-account-1")

    def test_logout_clears_cookie(self) -> None:
        self.login()

        response = self.client.post("/auth/logout")
        protected = self.client.get("/auth/me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "logged_out")
        self.assertIn(f"{AUTH_COOKIE_NAME}=", response.headers["set-cookie"])
        self.assertEqual(protected.status_code, 401)

    def test_frontend_pages_surface_expected_integration_boundaries(self) -> None:
        login_page = self.client.get("/")
        dashboard_page = self.client.get("/dashboard")

        self.assertEqual(login_page.status_code, 200)
        self.assertIn("/auth/login", login_page.text)
        self.assertIn("Sandbox/demo only", login_page.text)
        self.assertEqual(dashboard_page.status_code, 200)
        self.assertIn("https://cdn.plaid.com/link/v2/stable/link-initialize.js", dashboard_page.text)
        self.assertIn("Plaid Link handles bank sign-in", dashboard_page.text)

    def test_in_memory_transaction_fallback_is_used_for_non_live_accounts(self) -> None:
        self.login()
        account = self.store.create_account(
            user_id="demo-user",
            account_number="000111222333",
            account_type="checking",
            balance="10.00",
        )
        self.store.add_transaction("demo-user", account.account_number, "5.00", "deposit")

        response = self.client.get(f"/bank/accounts/{account.account_number}/transactions")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "in-memory")
        self.assertEqual(response.json()["transactions"][0]["name"], "Deposit")

    def test_connector_errors_map_to_gateway_errors(self) -> None:
        class FailingConnector(FakePlaidConnector):
            def create_link_token(self, *, user_id: str):
                raise BankingAuthenticationError("plaid auth failed")

        app = create_app(
            env=self.env,
            account_store=self.store,
            connector_factory=FailingConnector,
        )
        client = TestClient(app)
        client.post("/auth/login", json={"username": "demo-user", "password": "demo-password"})

        response = client.post("/bank/link-token")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "plaid auth failed")


if __name__ == "__main__":
    unittest.main()
