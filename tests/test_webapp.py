import unittest

from fastapi.testclient import TestClient

from bank_account import AccountStatus, BankAccountStore
from banking_connector import AppCredentials
from webapp.app import create_app


class WebappTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = BankAccountStore()
        self.store.create_account(
            user_id="alice",
            account_number="000123456700",
            account_type="checking",
            balance="250.00",
            routing_number="990000000",
            bank_name="Live Build Bank",
        )
        self.store.create_account(
            user_id="alice",
            account_number="000123456701",
            account_type="savings",
            balance="100.00",
            routing_number="990000000",
            bank_name="Live Build Bank",
        )
        self.store.create_account(
            user_id="bob",
            account_number="000123456702",
            account_type="checking",
            balance="25.00",
            routing_number="990000000",
            bank_name="Live Build Bank",
        )
        auth_secret = "-".join(("secret", "password"))
        self.client = TestClient(
            create_app(
                store=self.store,
                credentials=AppCredentials("alice", auth_secret),
            )
        )

    def test_dashboard_requires_authentication(self) -> None:
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Basic")

    def test_dashboard_hides_routing_numbers(self) -> None:
        response = self.client.get("/dashboard", auth=("alice", "secret-password"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Payments dashboard for alice", response.text)
        self.assertNotIn("990000000", response.text)

    def test_payment_api_returns_receipt_and_updates_balances(self) -> None:
        response = self.client.post(
            "/api/payments",
            auth=("alice", "secret-password"),
            json={
                "source_account_number": "000123456700",
                "destination_user_id": "bob",
                "destination_account_number": "000123456702",
                "amount": "40.25",
                "memo": "Invoice 42",
            },
        )

        self.assertEqual(response.status_code, 200)
        receipt = response.json()["payment"]
        self.assertEqual(receipt["source_account_number"], "****6700")
        self.assertEqual(receipt["destination_account_number"], "****6702")
        self.assertEqual(receipt["amount"], "40.25")
        self.assertEqual(
            self.store.get_balance_status("alice", "000123456700")["balance"],
            "209.75",
        )
        self.assertEqual(
            self.store.get_balance_status("bob", "000123456702")["balance"],
            "65.25",
        )

    def test_payment_api_rejects_insufficient_funds(self) -> None:
        response = self.client.post(
            "/api/payments",
            auth=("alice", "secret-password"),
            json={
                "source_account_number": "000123456700",
                "destination_user_id": "bob",
                "destination_account_number": "000123456702",
                "amount": "500.00",
                "memo": "Too much",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "insufficient funds")

    def test_payment_api_rejects_invalid_amount(self) -> None:
        response = self.client.post(
            "/api/payments",
            auth=("alice", "secret-password"),
            json={
                "source_account_number": "000123456700",
                "destination_user_id": "bob",
                "destination_account_number": "000123456702",
                "amount": "1.999",
                "memo": "Bad amount",
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_payment_api_rejects_suspended_source_account(self) -> None:
        self.store.update_account("alice", "000123456700", status=AccountStatus.SUSPENDED)

        response = self.client.post(
            "/api/payments",
            auth=("alice", "secret-password"),
            json={
                "source_account_number": "000123456700",
                "destination_user_id": "bob",
                "destination_account_number": "000123456702",
                "amount": "10.00",
                "memo": "Suspended",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("source account must be active", response.json()["detail"])

    def test_payment_api_rejects_unowned_source_account(self) -> None:
        response = self.client.post(
            "/api/payments",
            auth=("alice", "secret-password"),
            json={
                "source_account_number": "000123456702",
                "destination_user_id": "alice",
                "destination_account_number": "000123456701",
                "amount": "10.00",
                "memo": "Unauthorized",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "source account is not owned by the authenticated user",
        )

    def test_payment_api_rejects_same_account(self) -> None:
        response = self.client.post(
            "/api/payments",
            auth=("alice", "secret-password"),
            json={
                "source_account_number": "000123456700",
                "destination_user_id": "alice",
                "destination_account_number": "000123456700",
                "amount": "10.00",
                "memo": "Self payment",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "source and destination accounts must differ",
        )

    def test_dashboard_payment_refreshes_balances_and_transactions(self) -> None:
        response = self.client.post(
            "/dashboard/payments",
            auth=("alice", "secret-password"),
            data={
                "source_account_number": "000123456700",
                "destination_user_id": "alice",
                "destination_account_number": "000123456701",
                "amount": "15.00",
                "memo": "Move to savings",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("submitted successfully", response.text)
        self.assertIn("Balance: $235.00", response.text)
        self.assertIn("Balance: $115.00", response.text)
        self.assertIn("payment_debit", response.text)
        self.assertIn("payment_credit", response.text)
        self.assertIn("Move to savings", response.text)


if __name__ == "__main__":
    unittest.main()
