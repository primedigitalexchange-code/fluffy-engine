"""In-memory storage and validation for bank account records."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import re
from threading import RLock
from typing import Any
from uuid import uuid4

from .models import AccountStatus, AccountStatusChange, AccountTransaction, BankAccount, utc_now

_ACCOUNT_NUMBER_RE = re.compile(r"^\d{6,34}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ACCOUNT_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")


class ValidationError(ValueError):
    """Raised when account or update payload data is invalid."""


class BankAccountStore:
    """Store and manage bank accounts with status and transaction history."""

    def __init__(self) -> None:
        """Initialize in-memory indices and account history storage."""

        self._lock = RLock()
        self._accounts_by_id: dict[str, BankAccount] = {}
        self._account_ids_by_user: dict[str, list[str]] = {}
        self._account_id_by_account_number: dict[str, str] = {}
        self._transactions_by_account: dict[str, list[AccountTransaction]] = {}
        self._status_history_by_account: dict[str, list[AccountStatusChange]] = {}

    def create_account(
        self,
        *,
        user_id: str,
        account_number: str,
        account_type: str,
        balance: Decimal | int | str = Decimal("0.00"),
        currency: str = "USD",
        status: AccountStatus | str = AccountStatus.ACTIVE,
    ) -> BankAccount:
        """Create and persist a new account for a user.

        Raises:
            ValidationError: If any account field fails validation.
        """

        normalized_user_id = _require_non_empty("user_id", user_id)
        normalized_account_number = _validate_account_number(account_number)
        normalized_account_type = _validate_account_type(account_type)
        normalized_balance = _validate_money(balance, field_name="balance", allow_zero=True)
        normalized_currency = _validate_currency(currency)
        normalized_status = _validate_status(status)

        with self._lock:
            if normalized_account_number in self._account_id_by_account_number:
                raise ValidationError("account number already exists")

            timestamp = utc_now()
            account = BankAccount(
                account_id=str(uuid4()),
                user_id=normalized_user_id,
                account_number=normalized_account_number,
                account_type=normalized_account_type,
                balance=normalized_balance,
                currency=normalized_currency,
                status=normalized_status,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._accounts_by_id[account.account_id] = account
            self._account_id_by_account_number[account.account_number] = account.account_id
            self._account_ids_by_user.setdefault(account.user_id, []).append(account.account_id)
            self._transactions_by_account[account.account_id] = []
            self._status_history_by_account[account.account_id] = []
            return account

    def get_account(self, account_id: str) -> BankAccount:
        """Return one account by its account_id.

        Raises:
            ValidationError: If account_id is empty.
            LookupError: If account is not found.
        """

        normalized_id = _require_non_empty("account_id", account_id)
        with self._lock:
            try:
                return self._accounts_by_id[normalized_id]
            except KeyError as exc:
                raise LookupError("account not found") from exc

    def list_accounts_for_user(self, user_id: str) -> list[BankAccount]:
        """Return all accounts for a user sorted by creation timestamp."""

        normalized_user_id = _require_non_empty("user_id", user_id)
        with self._lock:
            account_ids = self._account_ids_by_user.get(normalized_user_id, [])
            accounts = [self._accounts_by_id[account_id] for account_id in account_ids]
            return sorted(accounts, key=lambda account: account.created_at)

    def update_account(
        self,
        account_id: str,
        *,
        account_type: str | None = None,
        status: AccountStatus | str | None = None,
        status_reason: str | None = None,
    ) -> BankAccount:
        """Update allowed account fields while validating state transitions.

        Raises:
            ValidationError: If updates are invalid.
            LookupError: If account is not found.
        """

        with self._lock:
            account = self.get_account(account_id)
            updates: dict[str, Any] = {}
            normalized_reason = (
                _clean_optional_reason(status_reason, field_name="status_reason")
                if status_reason is not None
                else None
            )

            if normalized_reason is not None and status is None:
                raise ValidationError("status_reason can only be provided with a status update")

            if account_type is not None:
                updates["account_type"] = _validate_account_type(account_type)

            if status is not None:
                new_status = _validate_status(status)
                if normalized_reason is not None and new_status == account.status:
                    raise ValidationError("status_reason requires a status change")
                _validate_status_transition(account.status, new_status, normalized_reason)
                updates["status"] = new_status

            if not updates:
                raise ValidationError("at least one updatable field is required")

            updates["updated_at"] = utc_now()
            updated_account = replace(account, **updates)
            self._accounts_by_id[updated_account.account_id] = updated_account

            if "status" in updates and updates["status"] != account.status:
                self._status_history_by_account[account.account_id].append(
                    AccountStatusChange(
                        account_id=account.account_id,
                        old_status=account.status,
                        new_status=updates["status"],
                        changed_at=updated_account.updated_at,
                        reason=normalized_reason,
                    )
                )
            return updated_account

    def add_transaction(
        self,
        account_id: str,
        *,
        amount: Decimal | int | str,
        transaction_type: str,
        description: str | None = None,
    ) -> AccountTransaction:
        """Apply a deposit/withdrawal transaction and return the ledger entry.

        Raises:
            ValidationError: If transaction input is invalid.
            LookupError: If account is not found.
        """

        normalized_type = _require_non_empty("transaction_type", transaction_type).lower()
        if normalized_type not in {"deposit", "withdrawal", "transfer"}:
            raise ValidationError("transaction_type must be deposit, withdrawal, or transfer")

        normalized_amount = _validate_money(amount, field_name="amount", allow_zero=False)
        clean_description = _clean_optional_reason(description, field_name="description")

        with self._lock:
            account = self.get_account(account_id)
            if account.status != AccountStatus.ACTIVE:
                raise ValidationError("account must be active to accept transactions")

            resulting_balance = account.balance + normalized_amount
            if normalized_type in {"withdrawal", "transfer"}:
                resulting_balance = account.balance - normalized_amount
                if resulting_balance < Decimal("0.00"):
                    raise ValidationError("insufficient funds")

            updated_account = replace(account, balance=resulting_balance, updated_at=utc_now())
            self._accounts_by_id[account.account_id] = updated_account

            transaction = AccountTransaction(
                transaction_id=str(uuid4()),
                account_id=account.account_id,
                amount=normalized_amount,
                transaction_type=normalized_type,
                timestamp=updated_account.updated_at,
                resulting_balance=updated_account.balance,
                description=clean_description,
            )
            self._transactions_by_account[account.account_id].append(transaction)
            return transaction

    def list_transactions(self, account_id: str) -> list[AccountTransaction]:
        """Return account transaction history in insertion order."""

        account = self.get_account(account_id)
        with self._lock:
            return list(self._transactions_by_account[account.account_id])

    def get_status_history(self, account_id: str) -> list[AccountStatusChange]:
        """Return account status change history."""

        account = self.get_account(account_id)
        with self._lock:
            return list(self._status_history_by_account[account.account_id])


def _require_non_empty(field_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field_name} must be a non-empty string")
    return normalized


def _validate_account_number(account_number: Any) -> str:
    normalized = _require_non_empty("account_number", account_number)
    if not _ACCOUNT_NUMBER_RE.fullmatch(normalized):
        raise ValidationError("account_number must be 6-34 digits")
    return normalized


def _validate_account_type(account_type: Any) -> str:
    normalized = _require_non_empty("account_type", account_type).lower()
    if not _ACCOUNT_TYPE_RE.fullmatch(normalized):
        raise ValidationError("account_type must start with a letter and be 2-32 characters")
    return normalized


def _validate_status(status: AccountStatus | str) -> AccountStatus:
    if isinstance(status, AccountStatus):
        return status
    try:
        return AccountStatus(_require_non_empty("status", str(status)).lower())
    except ValueError as exc:
        raise ValidationError("status must be active, inactive, or suspended") from exc


def _validate_currency(currency: Any) -> str:
    normalized = _require_non_empty("currency", currency).upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise ValidationError("currency must be a 3-letter ISO-style code")
    return normalized


def _validate_money(value: Any, *, field_name: str, allow_zero: bool) -> Decimal:
    if isinstance(value, float):
        raise ValidationError(f"{field_name} must use Decimal, int, or string")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid decimal amount") from exc
    if not amount.is_finite():
        raise ValidationError(f"{field_name} must be a finite decimal amount")
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValidationError(f"{field_name} must be a valid decimal amount") from exc
    if quantized != amount:
        raise ValidationError(f"{field_name} must have at most 2 decimal places")
    if quantized < Decimal("0.00") or (not allow_zero and quantized == Decimal("0.00")):
        comparator = "greater than" if not allow_zero else "greater than or equal to"
        raise ValidationError(f"{field_name} must be {comparator} 0")
    return quantized


def _clean_optional_reason(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    cleaned = _require_non_empty(field_name, value)
    return cleaned


def _validate_status_transition(
    old_status: AccountStatus,
    new_status: AccountStatus,
    status_reason: str | None,
) -> None:
    if old_status == new_status:
        return
    if old_status == AccountStatus.SUSPENDED and new_status == AccountStatus.ACTIVE:
        if _clean_optional_reason(status_reason, field_name="status_reason") is None:
            raise ValidationError("status_reason is required to reactivate a suspended account")
