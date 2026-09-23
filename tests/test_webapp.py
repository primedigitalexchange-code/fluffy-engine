from __future__ import annotations

from datetime import UTC, datetime, timedelta
import base64
from decimal import Decimal
import hashlib
import json
import unittest

import jwt
from fastapi.testclient import TestClient

from bank_account import BankAccountStore
from banking_connector import (
    BankingAuthenticationError,
    ConnectedBankSession,
    LiveBankAccountData,
    TransferAuthorizationResult,
    TransferCreationResult,
    TransferStatusResult,
)
from webapp.app import AUTH_COOKIE_NAME, create_app


class FakePlaidConnector:
    def __init__(self) -> None:
        self.link_token_calls: list[str] = []
        self.connected: list[tuple[str, str]] = []
        self.transaction_calls: list[dict[str, object]] = []
        self.authorization_calls: list[dict[str, object]] = []
        self.transfer_create_calls: list[dict[str, object]] = []
        self.transfer_get_calls: list[str] = []
        self.authorization_decision = "approved"
        self.authorization_rationale = "approved"
        self.transfer_status = "pending"
        self.transfer_network = "ach"
        self.webhook_secret = "webhook-secret"

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

    def create_transfer_authorization(self, **kwargs):
        self.authorization_calls.append(kwargs)
        return TransferAuthorizationResult(
            authorization_id="authorization-1",
            decision=self.authorization_decision,
            decision_rationale=self.authorization_rationale,
        )

    def create_transfer(self, **kwargs):
        self.transfer_create_calls.append(kwargs)
        return TransferCreationResult(
            transfer_id="transfer-1",
            status=self.transfer_status,
            ach_class=kwargs["ach_class"],
            network=self.transfer_network,
        )

    def get_transfer_status(self, transfer_id: str):
        self.transfer_get_calls.append(transfer_id)
        return TransferStatusResult(
            transfer_id=transfer_id,
            status="posted",
            ach_class="web",
            network="ach",
        )

    def get_webhook_verification_key(self, key_id: str):
        encoded_secret = base64.urlsafe_b64encode(self.webhook_secret.encode("utf-8")).decode("utf-8").rstrip("=")
        return {"kty": "oct", "kid": key_id, "alg": "HS256", "k": encoded_secret}


def json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BankAccountStore()
        self.connector = FakePlaidConnector()
        self.env = {
            "APP_USERNAME": "demo-user",
            "APP_PASSWORD": "demo-password",
            "APP_JWT_SECRET": "test-jwt-secret",
            "APP_CORS_ORIGINS": "http://localhost:3000,http://127.0.0.1:8000",
            "FLUFFY_ENGINE_BANK_DATA_MODE": "live",
            "FLUFFY_ENGINE_ENABLE_LIVE_TRANSFERS": "true",
            "PLAID_WEBHOOK_URL": "https://example.com/webhooks/plaid/transfer",
            "PLAID_WEBHOOK_AUDIENCE": "https://example.com/webhooks/plaid/transfer",
            "FLUFFY_ENGINE_TRANSFER_STATUS_STALE_SECONDS": "300",
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

    def link_live_account(self) -> str:
        exchange_response = self.client.post(
            "/bank/exchange-token",
            json={"public_token": "public-sandbox-token"},
        )
        self.assertEqual(exchange_response.status_code, 200)
        return exchange_response.json()["accounts"][0]["account_id"]

    def signed_webhook(self, payload: dict[str, object], *, kid: str = "kid-1") -> str:
        payload_bytes = json_bytes(payload)
        claims = {
            "aud": self.env["PLAID_WEBHOOK_AUDIENCE"],
            "request_body_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        }
        return jwt.encode(claims, self.connector.webhook_secret, algorithm="HS256", headers={"kid": kid})

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

    def test_live_transfer_creation_returns_safe_receipt(self) -> None:
        self.login()
        source_account_id = self.link_live_account()
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000999888777",
            account_type="checking",
            balance="0.00",
        )

        response = self.client.post(
            "/bank/transfers",
            json={
                "source_account_id": source_account_id,
                "destination_user_id": "recipient",
                "destination_account_id": destination.account_number,
                "amount": "10.00",
                "ach_class": "web",
                "idempotency_key": "idem-transfer-1",
                "memo": "Payroll",
            },
        )

        self.assertEqual(response.status_code, 200)
        transfer = response.json()["transfer"]
        self.assertEqual(transfer["status"], "pending")
        self.assertEqual(transfer["authorization_id"], "authorization-1")
        self.assertEqual(transfer["transfer_id"], "transfer-1")
        self.assertTrue(transfer["source_account"].startswith("****"))
        self.assertEqual(len(self.connector.transfer_create_calls), 1)

    def test_live_transfer_declined_authorization_is_persisted_without_transfer_creation(self) -> None:
        self.login()
        source_account_id = self.link_live_account()
        self.connector.authorization_decision = "declined"
        self.connector.authorization_rationale = "risk_review"
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000999888776",
            account_type="checking",
            balance="0.00",
        )

        response = self.client.post(
            "/bank/transfers",
            json={
                "source_account_id": source_account_id,
                "destination_user_id": "recipient",
                "destination_account_id": destination.account_number,
                "amount": "10.00",
                "ach_class": "ppd",
                "idempotency_key": "idem-transfer-2",
            },
        )

        self.assertEqual(response.status_code, 200)
        transfer = response.json()["transfer"]
        self.assertEqual(transfer["status"], "declined")
        self.assertEqual(transfer["transfer_id"], None)
        self.assertEqual(transfer["decision_rationale"], "risk_review")
        self.assertEqual(len(self.connector.transfer_create_calls), 0)

    def test_transfer_idempotency_replay_does_not_create_duplicate_plaid_transfer(self) -> None:
        self.login()
        source_account_id = self.link_live_account()
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000999888775",
            account_type="checking",
            balance="0.00",
        )
        payload = {
            "source_account_id": source_account_id,
            "destination_user_id": "recipient",
            "destination_account_id": destination.account_number,
            "amount": "10.00",
            "ach_class": "web",
            "idempotency_key": "idem-transfer-3",
        }

        first = self.client.post("/bank/transfers", json=payload)
        second = self.client.post("/bank/transfers", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["idempotent_replay"], True)
        self.assertEqual(len(self.connector.transfer_create_calls), 1)

    def test_transfer_webhook_valid_signature_updates_status(self) -> None:
        self.login()
        source_account_id = self.link_live_account()
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000999888774",
            account_type="checking",
            balance="0.00",
        )
        create_response = self.client.post(
            "/bank/transfers",
            json={
                "source_account_id": source_account_id,
                "destination_user_id": "recipient",
                "destination_account_id": destination.account_number,
                "amount": "10.00",
                "ach_class": "web",
                "idempotency_key": "idem-transfer-4",
            },
        )
        payment_id = create_response.json()["transfer"]["payment_id"]
        posted_payload = {"event": {"transfer_id": "transfer-1", "event_type": "posted"}}

        webhook_response = self.client.post(
            "/webhooks/plaid/transfer",
            data=json_bytes(posted_payload),
            headers={
                "Content-Type": "application/json",
                "Plaid-Verification": self.signed_webhook(posted_payload),
            },
        )
        status_response = self.client.get(f"/bank/transfers/{payment_id}")

        self.assertEqual(webhook_response.status_code, 200)
        self.assertEqual(webhook_response.json()["status"], "processed")
        self.assertEqual(status_response.json()["transfer"]["status"], "posted")

    def test_transfer_webhook_rejects_tampered_payload(self) -> None:
        self.login()
        self.link_live_account()
        payload = {"event": {"transfer_id": "transfer-1", "event_type": "posted"}}
        tampered_payload = {"event": {"transfer_id": "transfer-1", "event_type": "failed"}}
        signature = self.signed_webhook(payload)

        response = self.client.post(
            "/webhooks/plaid/transfer",
            data=json_bytes(tampered_payload),
            headers={"Content-Type": "application/json", "Plaid-Verification": signature},
        )

        self.assertEqual(response.status_code, 401)

    def test_transfer_webhook_updates_status_to_returned(self) -> None:
        self.login()
        source_account_id = self.link_live_account()
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000999888771",
            account_type="checking",
            balance="0.00",
        )
        create_response = self.client.post(
            "/bank/transfers",
            json={
                "source_account_id": source_account_id,
                "destination_user_id": "recipient",
                "destination_account_id": destination.account_number,
                "amount": "10.00",
                "ach_class": "web",
                "idempotency_key": "idem-transfer-returned",
            },
        )
        payment_id = create_response.json()["transfer"]["payment_id"]
        returned_payload = {"event": {"transfer_id": "transfer-1", "event_type": "returned"}}
        self.client.post(
            "/webhooks/plaid/transfer",
            data=json_bytes(returned_payload),
            headers={
                "Content-Type": "application/json",
                "Plaid-Verification": self.signed_webhook(returned_payload),
            },
        )

        status_response = self.client.get(f"/bank/transfers/{payment_id}")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["transfer"]["status"], "returned")

    def test_transfer_status_endpoint_refreshes_stale_pending_transfer(self) -> None:
        self.login()
        source_account_id = self.link_live_account()
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000999888773",
            account_type="checking",
            balance="0.00",
        )
        create_response = self.client.post(
            "/bank/transfers",
            json={
                "source_account_id": source_account_id,
                "destination_user_id": "recipient",
                "destination_account_id": destination.account_number,
                "amount": "10.00",
                "ach_class": "web",
                "idempotency_key": "idem-transfer-5",
            },
        )
        payment_id = create_response.json()["transfer"]["payment_id"]
        self.store.update_live_transfer(
            payment_id,
            updated_at=datetime.now(UTC) - timedelta(hours=1),
        )
        status_response = self.client.get(f"/bank/transfers/{payment_id}")

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["transfer"]["status"], "posted")
        self.assertEqual(self.connector.transfer_get_calls, ["transfer-1"])

    def test_live_transfer_request_is_rejected_when_feature_disabled(self) -> None:
        env = dict(self.env)
        env["FLUFFY_ENGINE_ENABLE_LIVE_TRANSFERS"] = "false"
        app = create_app(env=env, account_store=self.store, connector_factory=lambda: self.connector)
        client = TestClient(app)
        client.post("/auth/login", json={"username": "demo-user", "password": "demo-password"})
        exchange = client.post("/bank/exchange-token", json={"public_token": "public-sandbox-token"})
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000999888772",
            account_type="checking",
            balance="0.00",
        )

        response = client.post(
            "/bank/transfers",
            json={
                "source_account_id": exchange.json()["accounts"][0]["account_id"],
                "destination_user_id": "recipient",
                "destination_account_id": destination.account_number,
                "amount": "10.00",
                "ach_class": "web",
                "idempotency_key": "idem-transfer-6",
            },
        )

        self.assertEqual(response.status_code, 503)

    def test_non_live_transfer_still_uses_in_memory_path(self) -> None:
        self.login()
        source = self.store.create_account(
            user_id="demo-user",
            account_number="000555444333",
            account_type="checking",
            balance="15.00",
        )
        destination = self.store.create_account(
            user_id="recipient",
            account_number="000111000999",
            account_type="checking",
            balance="1.00",
        )

        response = self.client.post(
            "/bank/transfers",
            json={
                "source_account_id": source.account_number,
                "destination_user_id": "recipient",
                "destination_account_id": destination.account_number,
                "amount": "5.00",
                "ach_class": "web",
                "idempotency_key": "idem-demo-1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transfer"]["data_source"], "in-memory")
        self.assertEqual(self.store.get_account("demo-user", source.account_number).balance, Decimal("10.00"))
        self.assertEqual(self.store.get_account("recipient", destination.account_number).balance, Decimal("6.00"))

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
