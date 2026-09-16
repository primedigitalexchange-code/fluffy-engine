"""In-memory bank account module.

This module stores and manages bank accounts per user, supports status
tracking, and records account transactions.

Configuration requirements:
- No external services are required.
- Data is in-memory only and resets when the process exits.

Usage example:
    >>> from decimal import Decimal
    >>> from bank_account.module import BankAccount, BankAccountStore
    >>> store = BankAccountStore()
    >>> account = BankAccount(
    ...     user_id="user-1",
    ...     account_number="123456789012",
    ...     account_type="checking",
    ...     balance=Decimal("100.00"),
    ... )
    >>> store.create_account(account)
    BankAccount(user_id='user-1', account_number='123456789012', account_type='checking', balance=Decimal('100.00'), status=<AccountStatus.ACTIVE: 'active'>)
    >>> store.get_balance_status("user-1", "123456789012")
    {'balance': '100.00', 'status': 'active'}
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import uuid4

CURRENCY_SCALE = Decimal("0.01")


class ValidationError(ValueError):
    """Raised when account input data is invalid."""


class NotFoundError(LookupError):
    """Raised when an account cannot be found."""


class AccountStatus(str, Enum):
    """Supported account states."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class BankAccount:
    """Bank account data model."""

    user_id: str
    account_number: str
    account_type: str
    balance: Decimal
    status: AccountStatus = AccountStatus.ACTIVE


@dataclass(frozen=True)
class AccountTransaction:
    """Account transaction model."""

    transaction_id: str
    account_number: str
    amount: Decimal
    transaction_type: str
    created_at: datetime


class BankAccountStore:
    """Store and manage multiple bank accounts per user."""

    def __init__(self) -> None:
        self._accounts: dict[str, dict[str, BankAccount]] = {}
        self._transactions: dict[str, list[AccountTransaction]] = {}

    def create_account(self, account: BankAccount) -> BankAccount:
        """Create and store a new account after validation."""
        self._validate_account(account)
        if any(
            account.account_number in user_accounts
            for user_accounts in self._accounts.values()
        ):
            raise ValidationError("account_number must be globally unique")
        user_accounts = self._accounts.setdefault(account.user_id, {})
        user_accounts[account.account_number] = account
        self._transactions.setdefault(account.account_number, [])
        return account

    def list_accounts(self, user_id: str) -> list[BankAccount]:
        """List all accounts for a user."""
        if not user_id.strip():
            raise ValidationError("user_id is required")
        return list(self._accounts.get(user_id, {}).values())

    def get_account(self, user_id: str, account_number: str) -> BankAccount:
        """Retrieve an account if owned by the provided user."""
        self._validate_user_and_account_number(user_id, account_number)

        try:
            return self._accounts[user_id][account_number]
        except KeyError as error:
            raise NotFoundError("Account not found") from error

    def update_account(
        self,
        user_id: str,
        account_number: str,
        *,
        account_type: str | None = None,
        status: AccountStatus | str | None = None,
    ) -> BankAccount:
        """Update account details and return the updated account."""
        current = self.get_account(user_id, account_number)

        normalized_status = current.status
        if status is not None:
            try:
                normalized_status = AccountStatus(status)
            except ValueError as error:
                raise ValidationError("Invalid account status") from error

        updated_account_type = account_type if account_type is not None else current.account_type
        if not updated_account_type.strip():
            raise ValidationError("account_type must be non-empty")

        updated = BankAccount(
            user_id=current.user_id,
            account_number=current.account_number,
            account_type=updated_account_type,
            balance=current.balance,
            status=normalized_status,
        )
        self._accounts[user_id][account_number] = updated
        return updated

    def get_balance_status(self, user_id: str, account_number: str) -> dict[str, str]:
        """Get balance and status for an account."""
        account = self.get_account(user_id, account_number)
        return {"balance": f"{account.balance:.2f}", "status": account.status.value}

    def add_transaction(
        self,
        user_id: str,
        account_number: str,
        *,
        amount: Decimal | str,
        transaction_type: str,
    ) -> AccountTransaction:
        """Record a transaction and update account balance."""
        account = self.get_account(user_id, account_number)
        if account.status != AccountStatus.ACTIVE:
            raise ValidationError("Cannot transact on non-active account")

        normalized_amount = self._to_decimal(amount, field_name="amount")
        if normalized_amount <= Decimal("0"):
            raise ValidationError("amount must be positive")

        normalized_type = transaction_type.strip().lower()
        if normalized_type not in {"credit", "debit"}:
            raise ValidationError("transaction_type must be 'credit' or 'debit'")

        new_balance = (
            account.balance + normalized_amount
            if normalized_type == "credit"
            else account.balance - normalized_amount
        )
        if new_balance < Decimal("0"):
            raise ValidationError("Insufficient funds for debit transaction")

        updated = BankAccount(
            user_id=account.user_id,
            account_number=account.account_number,
            account_type=account.account_type,
            balance=new_balance,
            status=account.status,
        )
        self._accounts[user_id][account_number] = updated

        transaction = AccountTransaction(
            transaction_id=str(uuid4()),
            account_number=account_number,
            amount=normalized_amount,
            transaction_type=normalized_type,
            created_at=datetime.now(timezone.utc),
        )
        self._transactions[account_number].append(transaction)
        return transaction

    def list_transactions(self, user_id: str, account_number: str) -> list[AccountTransaction]:
        """List transactions for an account."""
        self.get_account(user_id, account_number)
        return list(self._transactions.get(account_number, []))

    @staticmethod
    def _validate_account(account: BankAccount) -> None:
        if not account.user_id.strip():
            raise ValidationError("user_id must be non-empty")
        if not account.account_type.strip():
            raise ValidationError("account_type must be non-empty")
        if not (account.account_number.isdigit() and 10 <= len(account.account_number) <= 18):
            raise ValidationError("account_number must be 10-18 digits")
        if account.balance < Decimal("0"):
            raise ValidationError("balance cannot be negative")
        if account.balance != account.balance.quantize(CURRENCY_SCALE):
            raise ValidationError("balance must use two decimal places")

    @staticmethod
    def _validate_user_and_account_number(user_id: str, account_number: str) -> None:
        if not user_id.strip():
            raise ValidationError("user_id is required")
        if not account_number.strip():
            raise ValidationError("account_number is required")

    @staticmethod
    def _to_decimal(value: Decimal | str, *, field_name: str) -> Decimal:
        if isinstance(value, Decimal):
            if value != value.quantize(CURRENCY_SCALE):
                raise ValidationError(f"{field_name} must use two decimal places")
            return value
        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be provided as a decimal string")
        try:
            decimal_value = Decimal(value)
        except (InvalidOperation, TypeError) as error:
            raise ValidationError(f"{field_name} must be a valid decimal value") from error
        if decimal_value != decimal_value.quantize(CURRENCY_SCALE):
            raise ValidationError(f"{field_name} must use two decimal places")
        return decimal_value


def retrieve_account(store: BankAccountStore, user_id: str, account_number: str) -> dict[str, str]:
    """API-style endpoint to retrieve account information."""
    account = store.get_account(user_id, account_number)
    payload = asdict(account)
    payload["balance"] = f"{account.balance:.2f}"
    payload["status"] = account.status.value
    return payload


def update_account(
    store: BankAccountStore,
    user_id: str,
    account_number: str,
    payload: dict[str, str],
) -> dict[str, str]:
    """API-style endpoint to update account details."""
    account_type = payload.get("account_type")
    status = payload.get("status")
    updated = store.update_account(
        user_id,
        account_number,
        account_type=account_type,
        status=status,
    )
    response = asdict(updated)
    response["balance"] = f"{updated.balance:.2f}"
    response["status"] = updated.status.value
    return response


def list_user_accounts(store: BankAccountStore, user_id: str) -> list[dict[str, str]]:
    """API-style endpoint to list all accounts for a user."""
    accounts = store.list_accounts(user_id)
    payload: list[dict[str, str]] = []
    for account in accounts:
        serialized = asdict(account)
        serialized["balance"] = f"{account.balance:.2f}"
        serialized["status"] = account.status.value
        payload.append(serialized)
    return payload


def get_account_balance_status(
    store: BankAccountStore,
    user_id: str,
    account_number: str,
) -> dict[str, str]:
    """API-style endpoint to get account balance and status."""
    return store.get_balance_status(user_id, account_number)
