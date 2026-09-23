"""Compatibility store exports for bank_accounts."""

from __future__ import annotations

from decimal import Decimal

from bank_account import NotFoundError, ValidationError
from bank_account.module import BankAccountStore as CanonicalBankAccountStore


class BankAccountStore(CanonicalBankAccountStore):
    """Compatibility wrapper exposing the draft account_id-based store API."""

    def get_account(self, account_id: str):  # type: ignore[override]
        return super().get_account_by_id(account_id)

    def list_accounts_for_user(self, user_id: str):  # type: ignore[override]
        return super().list_accounts(user_id)

    def update_account(self, account_id: str, **updates):  # type: ignore[override]
        return super().update_account_by_id(account_id, **updates)

    def add_transaction(
        self,
        account_id: str,
        *,
        amount: Decimal | int | str,
        transaction_type: str,
        description: str | None = None,
    ):  # type: ignore[override]
        return super().add_transaction_by_id(
            account_id,
            amount=amount,
            transaction_type=transaction_type,
            description=description,
        )

    def list_transactions(self, account_id: str):  # type: ignore[override]
        return super().list_transactions_by_id(account_id)

    def get_status_history(self, account_id: str):  # type: ignore[override]
        return super().get_status_history_by_id(account_id)

    def get_balance_status(self, account_id: str):  # type: ignore[override]
        return super().get_balance_status_by_id(account_id)


__all__ = [
    "BankAccountStore",
    "NotFoundError",
    "ValidationError",
]
