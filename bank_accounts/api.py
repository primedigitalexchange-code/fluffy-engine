"""Compatibility API exports for bank_accounts."""

from __future__ import annotations

from typing import Any

from bank_account import APIError
from bank_account import get_account_audit_history as canonical_get_account_audit_history
from bank_account import get_account_balance_status as canonical_get_account_balance_status
from bank_account import handle_request as canonical_handle_request
from bank_account import list_user_accounts as canonical_list_user_accounts
from bank_account import retrieve_account as canonical_retrieve_account
from bank_account import update_account as canonical_update_account


def _canonical_store(store: Any) -> Any:
    return getattr(store, "canonical_store", store)


def handle_request(store: Any, **kwargs: Any):
    return canonical_handle_request(_canonical_store(store), **kwargs)


def retrieve_account(store: Any, user_id: str, account_number: str):
    return canonical_retrieve_account(_canonical_store(store), user_id, account_number)


def update_account(store: Any, user_id: str, account_number: str, **updates: Any):
    return canonical_update_account(_canonical_store(store), user_id, account_number, **updates)


def list_user_accounts(store: Any, user_id: str):
    return canonical_list_user_accounts(_canonical_store(store), user_id)


def get_account_balance_status(store: Any, user_id: str, account_number: str):
    return canonical_get_account_balance_status(_canonical_store(store), user_id, account_number)


def get_account_audit_history(store: Any, user_id: str, account_number: str):
    return canonical_get_account_audit_history(_canonical_store(store), user_id, account_number)

__all__ = [
    "APIError",
    "get_account_audit_history",
    "get_account_balance_status",
    "handle_request",
    "list_user_accounts",
    "retrieve_account",
    "update_account",
]
