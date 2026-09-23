from __future__ import annotations

from datetime import date
import io
import json
import unittest
from urllib.error import HTTPError

from banking_connector import (
    AppCredentials,
    BankingAuthenticationError,
    BankingConfig,
    ConfigurationError,
    EncryptedTokenStore,
    PlaidConnector,
    load_app_credentials_from_env,
)


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class RecordingOpener:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def __call__(self, request, timeout: int):
        self.requests.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeHTTPResponse(response)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class BankingConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BankingConfig.from_env(
            {
                "FLUFFY_ENGINE_BANK_DATA_MODE": "live",
                "FLUFFY_ENGINE_BANK_PROVIDER": "plaid",
                "PLAID_CLIENT_ID": "client-id",
                "PLAID_SECRET": "secret-value",
                "PLAID_ENV": "sandbox",
                "BANKING_TOKEN_ENCRYPTION_KEY": "unit-test-token-encryption-key",
                "BANKING_RATE_LIMIT_PER_MINUTE": "30",
            }
        )

    def test_live_configuration_requires_credentials(self) -> None:
        with self.assertRaises(ConfigurationError):
            BankingConfig.from_env({"FLUFFY_ENGINE_BANK_DATA_MODE": "live"})

    def test_app_credentials_load_from_environment(self) -> None:
        credentials = AppCredentials.from_env(
            {
                "APP_USERNAME": "app-user@example.com",
                "APP_PASSWORD": "top-secret-value",
            }
        )

        self.assertEqual(credentials.username, "app-user@example.com")
        self.assertEqual(credentials.password, "top-secret-value")

    def test_app_credentials_require_both_environment_variables(self) -> None:
        with self.assertRaises(ConfigurationError) as raised:
            AppCredentials.from_env({"APP_USERNAME": "app-user@example.com"})

        self.assertIn("APP_PASSWORD", str(raised.exception))

    def test_load_app_credentials_from_env_returns_app_credentials(self) -> None:
        credentials = load_app_credentials_from_env(
            {
                "APP_USERNAME": "app-user@example.com",
                "APP_PASSWORD": "top-secret-value",
            }
        )

        self.assertEqual(credentials, AppCredentials("app-user@example.com", "top-secret-value"))

    def test_token_store_encrypts_tokens_at_rest(self) -> None:
        store = EncryptedTokenStore("unit-test-token-encryption-key")

        store.save_token("item-1", "access-sandbox-secret")

        self.assertNotEqual(store._tokens["item-1"], "access-sandbox-secret")
        self.assertEqual(store.load_token("item-1"), "access-sandbox-secret")

    def test_connect_account_returns_live_account_details(self) -> None:
        opener = RecordingOpener(
            [
                {"access_token": "access-sandbox-secret", "item_id": "item-1", "request_id": "request-1"},
                {
                    "numbers": {
                        "ach": [
                            {
                                "account_id": "account-1",
                                "account": "000123456789",
                                "routing": "990000000",
                            }
                        ]
                    }
                },
                {
                    "accounts": [
                        {
                            "account_id": "account-1",
                            "balances": {"available": 2485.77, "current": 2485.77},
                            "subtype": "checking",
                        }
                    ]
                },
                {"item": {"institution_id": "ins_123"}},
                {"institution": {"name": "Live Build Bank"}},
            ]
        )
        connector = PlaidConnector(self.config, opener=opener)

        session = connector.connect_account(user_id="user-123", public_token="public-sandbox-good")

        self.assertEqual(session.item_id, "item-1")
        self.assertEqual(session.access_token_reference, "item-1")
        self.assertEqual(len(session.accounts), 1)
        account = session.accounts[0]
        self.assertEqual(account.account_number, "000123456789")
        self.assertEqual(account.routing_number, "990000000")
        self.assertEqual(str(account.balance), "2485.77")
        self.assertEqual(account.bank_name, "Live Build Bank")
        self.assertEqual(opener.requests[0]["url"], "https://sandbox.plaid.com/item/public_token/exchange")

    def test_get_transaction_history_uses_account_filter(self) -> None:
        opener = RecordingOpener(
            [
                {
                    "transactions": [
                        {
                            "transaction_id": "txn-1",
                            "account_id": "account-1",
                            "amount": 12.34,
                            "name": "Coffee Shop",
                            "date": "2026-09-15",
                            "pending": False,
                            "iso_currency_code": "USD",
                            "merchant_name": "Coffee Shop",
                        }
                    ]
                }
            ]
        )
        token_store = EncryptedTokenStore("unit-test-token-encryption-key")
        token_store.save_token("item-1", "access-sandbox-secret")
        connector = PlaidConnector(self.config, opener=opener, token_store=token_store)

        transactions = connector.get_transaction_history(
            "item-1",
            start_date=date(2026, 9, 1),
            end_date="2026-09-15",
            account_id="account-1",
        )

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0].name, "Coffee Shop")
        self.assertEqual(str(transactions[0].amount), "12.34")
        self.assertEqual(
            opener.requests[0]["payload"]["options"],
            {"account_ids": ["account-1"]},
        )

    def test_refresh_access_token_replaces_stored_token(self) -> None:
        opener = RecordingOpener([{"new_access_token": "access-sandbox-new", "request_id": "request-2"}])
        token_store = EncryptedTokenStore("unit-test-token-encryption-key")
        token_store.save_token("item-1", "access-sandbox-old")
        connector = PlaidConnector(self.config, opener=opener, token_store=token_store)

        connector.refresh_access_token("item-1")

        self.assertEqual(token_store.load_token("item-1"), "access-sandbox-new")

    def test_rate_limit_waits_for_next_available_window(self) -> None:
        clock = FakeClock()
        config = BankingConfig.from_env(
            {
                "FLUFFY_ENGINE_BANK_DATA_MODE": "live",
                "FLUFFY_ENGINE_BANK_PROVIDER": "plaid",
                "PLAID_CLIENT_ID": "client-id",
                "PLAID_SECRET": "secret-value",
                "PLAID_ENV": "sandbox",
                "BANKING_TOKEN_ENCRYPTION_KEY": "unit-test-token-encryption-key",
                "BANKING_RATE_LIMIT_PER_MINUTE": "1",
            }
        )
        opener = RecordingOpener(
            [
                {"link_token": "link-1", "expiration": "2026-09-16T00:00:00Z", "request_id": "request-1"},
                {"link_token": "link-2", "expiration": "2026-09-16T00:01:00Z", "request_id": "request-2"},
            ]
        )
        connector = PlaidConnector(
            config,
            opener=opener,
            monotonic_clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        connector.create_link_token(user_id="user-123")
        connector.create_link_token(user_id="user-123")

        self.assertEqual(clock.sleeps, [60.0])

    def test_authentication_errors_are_sanitized(self) -> None:
        http_error = HTTPError(
            url="https://sandbox.plaid.com/item/public_token/exchange",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(
                b'{"error_message":"invalid token access-sandbox-secret for public-sandbox-bad"}'
            ),
        )
        opener = RecordingOpener([http_error])
        connector = PlaidConnector(self.config, opener=opener)

        with self.assertRaises(BankingAuthenticationError) as raised:
            connector.exchange_public_token("public-sandbox-bad")

        self.assertNotIn("access-sandbox-secret", str(raised.exception))
        self.assertNotIn("public-sandbox-bad", str(raised.exception))
        self.assertIn("[REDACTED_TOKEN]", str(raised.exception))

    # Example live integration test (kept commented out because it requires real Plaid keys):
    #
    # def test_live_plaid_integration_example(self) -> None:
    #     config = BankingConfig.from_env()
    #     connector = PlaidConnector(config)
    #     session = connector.connect_account(
    #         user_id="user-123",
    #         public_token=os.environ["PLAID_PUBLIC_TOKEN"],
    #     )
    #     self.assertGreaterEqual(len(session.accounts), 1)


if __name__ == "__main__":
    unittest.main()
