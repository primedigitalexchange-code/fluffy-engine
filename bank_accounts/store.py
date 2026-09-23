"""Compatibility store facade for bank_accounts."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bank_account import NotFoundError, ValidationError
from bank_account.module import BankAccountStore as CanonicalBankAccountStore


class BankAccountStore:
    """Wrap the canonical store with the draft account_id-based interface."""

    def __init__(self) -> None:
        self._store = CanonicalBankAccountStore()

    @property
    def canonical_store(self) -> CanonicalBankAccountStore:
        return self._store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def create_account(self, **kwargs: Any):
        return self._store.create_account(**kwargs)

    def create_account_from_mock_data(self, **kwargs: Any):
        return self._store.create_account_from_mock_data(**kwargs)

    def create_account_from_live_data(self, **kwargs: Any):
        return self._store.create_account_from_live_data(**kwargs)

    def get_account(self, account_id: str):
        return self._store.get_account_by_id(account_id)

    def list_accounts_for_user(self, user_id: str):
        return self._store.list_accounts(user_id)

    def update_account(self, account_id: str, **updates: Any):
        return self._store.update_account_by_id(account_id, **updates)

    def add_transaction(
        self,
        account_id: str,
        *,
        amount: Decimal | int | str,
        transaction_type: str,
        description: str | None = None,
    ):
        return self._store.add_transaction_by_id(
            account_id,
            amount=amount,
            transaction_type=transaction_type,
            description=description,
        )

    def list_transactions(self, account_id: str):
        return self._store.list_transactions_by_id(account_id)

    def get_status_history(self, account_id: str):
        return self._store.get_status_history_by_id(account_id)

    def get_balance_status(self, account_id: str):
        return self._store.get_balance_status_by_id(account_id)


__all__ = [
    "BankAccountStore",
    "NotFoundError",
    "ValidationError",
]
