"""HTTP-style endpoint handlers for bank account operations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .models import AccountStatusChange, AccountTransaction, BankAccount
from .store import BankAccountStore, ValidationError


class APIError(RuntimeError):
    """Error type carrying HTTP-like status codes for endpoint handlers."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def handle_request(
    store: BankAccountStore,
    *,
    method: str,
    path: str,
    user_id: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Handle an endpoint request for the bank account API.

    Supported routes:
    - GET /accounts/<account_id>
    - PATCH /accounts/<account_id>
    - GET /users/<user_id>/accounts
    - GET /accounts/<account_id>/balance
    """

    normalized_method = method.upper().strip()
    parts = [part for part in path.strip("/").split("/") if part]

    try:
        if normalized_method == "GET" and len(parts) == 2 and parts[0] == "accounts":
            account = store.get_account(parts[1])
            _ensure_ownership(requesting_user_id=user_id, owner_user_id=account.user_id)
            return 200, {"account": _serialize_account(account)}

        if normalized_method == "PATCH" and len(parts) == 2 and parts[0] == "accounts":
            account = store.get_account(parts[1])
            _ensure_ownership(requesting_user_id=user_id, owner_user_id=account.user_id)
            if payload is None:
                raise APIError(400, "payload is required")
            updated = store.update_account(
                account.account_id,
                account_type=payload.get("account_type"),
                status=payload.get("status"),
                status_reason=payload.get("status_reason"),
            )
            return 200, {"account": _serialize_account(updated)}

        if normalized_method == "GET" and len(parts) == 3 and parts[0] == "users" and parts[2] == "accounts":
            target_user_id = _normalize_user_id(parts[1])
            _ensure_ownership(requesting_user_id=user_id, owner_user_id=target_user_id)
            accounts = store.list_accounts_for_user(target_user_id)
            return 200, {"accounts": [_serialize_account(account) for account in accounts]}

        if normalized_method == "GET" and len(parts) == 3 and parts[0] == "accounts" and parts[2] == "balance":
            account = store.get_account(parts[1])
            _ensure_ownership(requesting_user_id=user_id, owner_user_id=account.user_id)
            return 200, {
                "account_id": account.account_id,
                "masked_account_number": account.masked_account_number,
                "balance": _serialize_decimal(account.balance),
                "currency": account.currency,
                "status": account.status.value,
            }

        raise APIError(404, "endpoint not found")
    except ValidationError as exc:
        return 400, {"error": str(exc)}
    except LookupError:
        return 404, {"error": "account not found"}
    except APIError as exc:
        return exc.status_code, {"error": exc.message}


def _ensure_ownership(*, requesting_user_id: str, owner_user_id: str) -> None:
    normalized_requesting_user_id = _normalize_user_id(requesting_user_id)
    normalized_owner_user_id = _normalize_user_id(owner_user_id)
    if normalized_requesting_user_id != normalized_owner_user_id:
        raise APIError(403, "forbidden: account does not belong to requesting user")


def _normalize_user_id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise APIError(400, "user_id must be a non-empty string")
    return normalized


def _serialize_account(account: BankAccount) -> dict[str, Any]:
    return {
        "account_id": account.account_id,
        "masked_account_number": account.masked_account_number,
        "account_type": account.account_type,
        "balance": _serialize_decimal(account.balance),
        "currency": account.currency,
        "status": account.status.value,
        "created_at": _serialize_datetime(account.created_at),
        "updated_at": _serialize_datetime(account.updated_at),
    }


def serialize_transaction(transaction: AccountTransaction) -> dict[str, Any]:
    """Serialize a transaction model for API-safe responses."""

    return {
        "transaction_id": transaction.transaction_id,
        "account_id": transaction.account_id,
        "amount": _serialize_decimal(transaction.amount),
        "transaction_type": transaction.transaction_type,
        "timestamp": _serialize_datetime(transaction.timestamp),
        "resulting_balance": _serialize_decimal(transaction.resulting_balance),
        "description": transaction.description,
    }


def serialize_status_change(change: AccountStatusChange) -> dict[str, Any]:
    """Serialize account status change audit entries."""

    return {
        "account_id": change.account_id,
        "old_status": change.old_status.value,
        "new_status": change.new_status.value,
        "changed_at": _serialize_datetime(change.changed_at),
        "reason": change.reason,
    }


def _serialize_decimal(value: Decimal) -> str:
    return f"{value:.2f}"


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()
