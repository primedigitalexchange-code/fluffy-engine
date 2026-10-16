"""Compatibility package for the canonical bank_account implementation."""

from bank_account import (
    APIError,
    AccountStatus,
    AccountStatusChange,
    AccountTransaction,
    BankAccount,
    DataSource,
    NotFoundError,
    ValidationError,
)
from .api import (
    create_account_from_live_api,
    create_account_from_mock_data,
    get_account_audit_history,
    get_account_balance_status,
    handle_request,
    list_user_accounts,
    retrieve_account,
    update_account,
)
from .store import BankAccountStore

__all__ = [
    "APIError",
    "AccountStatus",
    "AccountStatusChange",
    "AccountTransaction",
    "BankAccount",
    "BankAccountStore",
    "DataSource",
    "NotFoundError",
    "ValidationError",
    "create_account_from_live_api",
    "create_account_from_mock_data",
    "get_account_audit_history",
    "get_account_balance_status",
    "handle_request",
    "list_user_accounts",
    "retrieve_account",
    "update_account",
]
