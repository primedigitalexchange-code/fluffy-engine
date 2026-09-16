"""In-memory bank account models and helpers.

Example:
    >>> from bank_account import BankAccountStore
    >>> store = BankAccountStore()
    >>> account = store.create_account(
    ...     user_id="user-123",
    ...     account_number="000123456789",
    ...     account_type="checking",
    ...     balance="2485.77",
    ...     routing_number="990000000",
    ...     bank_name="Live Build Bank",
    ...     bank_address="742 Evergreen Terrace, Springfield, IL 62704, USA",
    ...     city="Springfield",
    ...     state="IL",
    ...     postal_code="62704",
    ... )
    >>> account.bank_name
    'Live Build Bank'
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from threading import RLock
from typing import Any
from uuid import uuid4

_ROUTING_NUMBER_RE = re.compile(r"^\d{9}$")
_POSTAL_CODE_RE = re.compile(r"^(?:\d{5}|\d{9}|\d{5}-\d{4})$")
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}


class ValidationError(ValueError):
    """Raised when account input fails validation."""


class NotFoundError(LookupError):
    """Raised when an account is missing for the given user."""


class AccountStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class BankAccount:
    user_id: str
    account_number: str
    account_type: str
    balance: Decimal
    status: AccountStatus = AccountStatus.ACTIVE
    routing_number: str | None = None
    bank_name: str | None = None
    bank_address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


@dataclass(frozen=True)
class AccountTransaction:
    transaction_id: str
    account_number: str
    amount: Decimal
    transaction_type: str
    created_at: datetime


def _validate_required_string(field_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field_name} must be a non-empty string")
    return normalized


def _validate_optional_string(field_name: str, value: Any) -> str | None:
    if value is None:
        return None
    normalized = _validate_required_string(field_name, value)
    if field_name == "routing_number" and not _ROUTING_NUMBER_RE.fullmatch(normalized):
        raise ValidationError("routing_number must be 9 digits")
    if field_name == "state":
        normalized = normalized.upper()
        if normalized not in _US_STATE_CODES:
            raise ValidationError("state must be a valid 2-letter US state code")
    if field_name == "postal_code" and not _POSTAL_CODE_RE.fullmatch(normalized):
        raise ValidationError("postal_code must be a 5-digit, 9-digit, or ZIP+4 US postal code")
    return normalized


def _validate_status(value: AccountStatus | str) -> AccountStatus:
    if isinstance(value, AccountStatus):
        return value
    try:
        return AccountStatus(str(value).strip().lower())
    except ValueError as exc:
        raise ValidationError("status must be active, inactive, or suspended") from exc


def _validate_balance(value: Decimal | int | float | str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("balance must be a valid decimal amount") from exc
    if amount < Decimal("0.00"):
        raise ValidationError("balance must be greater than or equal to 0")
    return amount


def _serialize_account(account: BankAccount) -> dict[str, Any]:
    return {
        "user_id": account.user_id,
        "account_number": account.account_number,
        "account_type": account.account_type,
        "balance": f"{account.balance:.2f}",
        "status": account.status.value,
        "routing_number": account.routing_number,
        "bank_name": account.bank_name,
        "bank_address": account.bank_address,
        "city": account.city,
        "state": account.state,
        "postal_code": account.postal_code,
    }


class BankAccountStore:
    """Manage bank accounts and simple in-memory transactions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._accounts: dict[tuple[str, str], BankAccount] = {}
        self._transactions: dict[tuple[str, str], list[AccountTransaction]] = {}

    def _get_account_unlocked(self, account_key: tuple[str, str]) -> BankAccount:
        try:
            return self._accounts[account_key]
        except KeyError as exc:
            raise NotFoundError("account not found") from exc

    def create_account(
        self,
        *,
        user_id: str,
        account_number: str,
        account_type: str,
        balance: Decimal | int | float | str = Decimal("0.00"),
        status: AccountStatus | str = AccountStatus.ACTIVE,
        routing_number: str | None = None,
        bank_name: str | None = None,
        bank_address: str | None = None,
        city: str | None = None,
        state: str | None = None,
        postal_code: str | None = None,
    ) -> BankAccount:
        user_id = _validate_required_string("user_id", user_id)
        account_number = _validate_required_string("account_number", account_number)
        account_type = _validate_required_string("account_type", account_type)
        account_key = (user_id, account_number)
        with self._lock:
            if account_key in self._accounts:
                raise ValidationError("account already exists for this user")

            account = BankAccount(
                user_id=user_id,
                account_number=account_number,
                account_type=account_type,
                balance=_validate_balance(balance),
                status=_validate_status(status),
                routing_number=_validate_optional_string("routing_number", routing_number),
                bank_name=_validate_optional_string("bank_name", bank_name),
                bank_address=_validate_optional_string("bank_address", bank_address),
                city=_validate_optional_string("city", city),
                state=_validate_optional_string("state", state),
                postal_code=_validate_optional_string("postal_code", postal_code),
            )
            self._accounts[account_key] = account
            self._transactions[account_key] = []
            return account

    def list_accounts(self, user_id: str) -> list[BankAccount]:
        user_id = _validate_required_string("user_id", user_id)
        with self._lock:
            return sorted(
                (
                    account
                    for (account_user_id, _), account in self._accounts.items()
                    if account_user_id == user_id
                ),
                key=lambda account: account.account_number,
            )

    def get_account(self, user_id: str, account_number: str) -> BankAccount:
        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_required_string("account_number", account_number),
        )
        with self._lock:
            return self._get_account_unlocked(account_key)

    def update_account(self, user_id: str, account_number: str, **updates: Any) -> BankAccount:
        supported_fields = {
            "account_type",
            "status",
            "routing_number",
            "bank_name",
            "bank_address",
            "city",
            "state",
            "postal_code",
        }
        unknown_fields = set(updates) - supported_fields
        if unknown_fields:
            unknown = ", ".join(sorted(unknown_fields))
            raise ValidationError(f"unsupported update field(s): {unknown}")

        normalized_updates: dict[str, Any] = {}
        if "account_type" in updates:
            normalized_updates["account_type"] = _validate_required_string(
                "account_type",
                updates["account_type"],
            )
        if "status" in updates:
            normalized_updates["status"] = _validate_status(updates["status"])

        for field_name in supported_fields - {"account_type", "status"}:
            if field_name in updates:
                normalized_updates[field_name] = _validate_optional_string(
                    field_name,
                    updates[field_name],
                )

        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_required_string("account_number", account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            updated_account = replace(account, **normalized_updates)
            self._accounts[account_key] = updated_account
            return updated_account

    def add_transaction(
        self,
        user_id: str,
        account_number: str,
        amount: Decimal | int | float | str,
        transaction_type: str,
    ) -> AccountTransaction:
        transaction_type = _validate_required_string("transaction_type", transaction_type).lower()
        if transaction_type not in {"deposit", "withdrawal"}:
            raise ValidationError("transaction_type must be deposit or withdrawal")

        amount_decimal = _validate_balance(amount)
        if amount_decimal == Decimal("0.00"):
            raise ValidationError("transaction amount must be greater than 0")

        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_required_string("account_number", account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            new_balance = account.balance + amount_decimal
            if transaction_type == "withdrawal":
                new_balance = account.balance - amount_decimal
                if new_balance < Decimal("0.00"):
                    raise ValidationError("insufficient funds")

            updated_account = replace(account, balance=new_balance)
            self._accounts[account_key] = updated_account

            transaction = AccountTransaction(
                transaction_id=str(uuid4()),
                account_number=updated_account.account_number,
                amount=amount_decimal,
                transaction_type=transaction_type,
                created_at=datetime.now(timezone.utc),
            )
            self._transactions[account_key].append(transaction)
            return transaction

    def list_transactions(self, user_id: str, account_number: str) -> list[AccountTransaction]:
        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_required_string("account_number", account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            return list(self._transactions[(account.user_id, account.account_number)])

    def get_balance_status(self, user_id: str, account_number: str) -> dict[str, str]:
        account = self.get_account(user_id, account_number)
        return {
            "account_number": account.account_number,
            "balance": f"{account.balance:.2f}",
            "status": account.status.value,
        }


def retrieve_account(store: BankAccountStore, user_id: str, account_number: str) -> dict[str, Any]:
    return {"account": _serialize_account(store.get_account(user_id, account_number))}


def update_account(
    store: BankAccountStore,
    user_id: str,
    account_number: str,
    **updates: Any,
) -> dict[str, Any]:
    return {"account": _serialize_account(store.update_account(user_id, account_number, **updates))}


def list_user_accounts(store: BankAccountStore, user_id: str) -> dict[str, list[dict[str, Any]]]:
    return {"accounts": [_serialize_account(account) for account in store.list_accounts(user_id)]}


def get_account_balance_status(
    store: BankAccountStore,
    user_id: str,
    account_number: str,
) -> dict[str, str]:
    return store.get_balance_status(user_id, account_number)
