"""In-memory bank account models and helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from threading import RLock
from typing import Any
from uuid import uuid4

from bank_profile import BankProfile

_ACCOUNT_NUMBER_RE = re.compile(r"^\d{6,34}$")
_ACCOUNT_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ROUTING_NUMBER_RE = re.compile(r"^\d{9}$")
_POSTAL_CODE_RE = re.compile(r"^(?:\d{5}|\d{9}|\d{5}-\d{4})$")
_BANK_DETAIL_FIELDS = (
    "routing_number",
    "bank_name",
    "bank_address",
    "city",
    "state",
    "postal_code",
)
_LIVE_METADATA_FIELDS = (
    "provider",
    "provider_item_id",
    "provider_account_id",
    "access_token_reference",
)
_UPDATABLE_ACCOUNT_FIELDS = (
    "account_type",
    "status",
    "status_reason",
    "data_source",
    "currency",
    *_BANK_DETAIL_FIELDS,
    *_LIVE_METADATA_FIELDS,
)
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}
_MISSING = object()


class ValidationError(ValueError):
    """Raised when account input fails validation."""


class NotFoundError(LookupError):
    """Raised when an account is missing for the given user."""


class APIError(RuntimeError):
    """Raised when HTTP-style account handling fails."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AccountStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class DataSource(str, Enum):
    MOCK = "mock"
    LIVE = "live"


@dataclass(frozen=True)
class BankAccount:
    user_id: str
    account_number: str
    account_type: str
    balance: Decimal
    status: AccountStatus = AccountStatus.ACTIVE
    data_source: DataSource = DataSource.MOCK
    routing_number: str | None = None
    bank_name: str | None = None
    bank_address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    provider: str | None = None
    provider_item_id: str | None = None
    provider_account_id: str | None = None
    access_token_reference: str | None = None
    currency: str = "USD"
    account_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def masked_account_number(self) -> str:
        return _mask_account_number(self.account_number)


@dataclass(frozen=True)
class AccountTransaction:
    transaction_id: str
    account_number: str
    amount: Decimal
    transaction_type: str
    created_at: datetime
    account_id: str = ""
    resulting_balance: Decimal | None = None
    description: str | None = None


@dataclass(frozen=True)
class AccountStatusChange:
    account_id: str
    account_number: str
    old_status: AccountStatus
    new_status: AccountStatus
    changed_at: datetime
    reason: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _validate_account_number(value: Any) -> str:
    normalized = _validate_required_string("account_number", value)
    if not _ACCOUNT_NUMBER_RE.fullmatch(normalized):
        raise ValidationError("account_number must be 6-34 digits")
    return normalized



def _validate_account_type(value: Any) -> str:
    normalized = _validate_required_string("account_type", value).lower()
    if not _ACCOUNT_TYPE_RE.fullmatch(normalized):
        raise ValidationError("account_type must start with a letter and be 2-32 characters")
    return normalized



def _validate_status(value: AccountStatus | str) -> AccountStatus:
    if isinstance(value, AccountStatus):
        return value
    try:
        return AccountStatus(str(value).strip().lower())
    except ValueError as exc:
        raise ValidationError("status must be active, inactive, or suspended") from exc



def _validate_data_source(value: DataSource | str) -> DataSource:
    if isinstance(value, DataSource):
        return value
    try:
        return DataSource(str(value).strip().lower())
    except ValueError as exc:
        raise ValidationError("data_source must be mock or live") from exc



def _validate_currency(value: Any) -> str:
    normalized = _validate_required_string("currency", value).upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise ValidationError("currency must be a 3-letter ISO-style code")
    return normalized



def _validate_money(field_name: str, value: Decimal | int | str, *, allow_zero: bool) -> Decimal:
    if isinstance(value, float):
        raise ValidationError("money values must be provided as Decimal, int, or string")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid decimal amount") from exc
    if not amount.is_finite():
        raise ValidationError(f"{field_name} must be a finite decimal amount")
    quantized_amount = amount.quantize(Decimal("0.01"))
    if quantized_amount != amount:
        raise ValidationError("money values must have no more than 2 decimal places")
    if quantized_amount < Decimal("0.00") or (not allow_zero and quantized_amount == Decimal("0.00")):
        comparator = "greater than" if not allow_zero else "greater than or equal to"
        raise ValidationError(f"{field_name} must be {comparator} 0")
    return quantized_amount



def _validate_live_requirements(account: BankAccount) -> None:
    if account.data_source != DataSource.LIVE:
        return
    if account.provider is None:
        raise ValidationError("provider is required when data_source is live")
    if account.access_token_reference is None:
        raise ValidationError("access_token_reference is required when data_source is live")



def _validate_status_transition(
    old_status: AccountStatus,
    new_status: AccountStatus,
    status_reason: str | None,
) -> None:
    if old_status == new_status:
        if status_reason is not None:
            raise ValidationError("status_reason requires a status change")
        return
    if old_status == AccountStatus.SUSPENDED and new_status == AccountStatus.ACTIVE and status_reason is None:
        raise ValidationError("status_reason is required to reactivate a suspended account")



def _normalize_bank_details(updates: dict[str, Any]) -> dict[str, str | None]:
    return {
        field_name: _validate_optional_string(field_name, updates[field_name])
        for field_name in _BANK_DETAIL_FIELDS
        if field_name in updates
    }



def _normalize_live_metadata(updates: dict[str, Any]) -> dict[str, str | None]:
    return {
        field_name: _validate_optional_string(field_name, updates[field_name])
        for field_name in _LIVE_METADATA_FIELDS
        if field_name in updates
    }



def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()



def _mask_account_number(account_number: str) -> str:
    suffix = account_number[-4:]
    return f"{'*' * max(len(account_number) - 4, 0)}{suffix}"



def _serialize_account(account: BankAccount) -> dict[str, Any]:
    return {
        "account_id": account.account_id,
        "user_id": account.user_id,
        "masked_account_number": account.masked_account_number,
        "account_type": account.account_type,
        "balance": f"{account.balance:.2f}",
        "currency": account.currency,
        "status": account.status.value,
        "data_source": account.data_source.value,
        "routing_number": account.routing_number,
        "bank_name": account.bank_name,
        "bank_address": account.bank_address,
        "city": account.city,
        "state": account.state,
        "postal_code": account.postal_code,
        "provider": account.provider,
        "provider_item_id": account.provider_item_id,
        "provider_account_id": account.provider_account_id,
        "created_at": _serialize_datetime(account.created_at),
        "updated_at": _serialize_datetime(account.updated_at),
    }



def _serialize_transaction(transaction: AccountTransaction) -> dict[str, Any]:
    return {
        "transaction_id": transaction.transaction_id,
        "account_id": transaction.account_id,
        "masked_account_number": _mask_account_number(transaction.account_number),
        "amount": f"{transaction.amount:.2f}",
        "transaction_type": transaction.transaction_type,
        "created_at": _serialize_datetime(transaction.created_at),
        "resulting_balance": (
            f"{transaction.resulting_balance:.2f}"
            if transaction.resulting_balance is not None
            else None
        ),
        "description": transaction.description,
    }



def _serialize_status_change(change: AccountStatusChange) -> dict[str, Any]:
    return {
        "account_id": change.account_id,
        "masked_account_number": _mask_account_number(change.account_number),
        "old_status": change.old_status.value,
        "new_status": change.new_status.value,
        "changed_at": _serialize_datetime(change.changed_at),
        "reason": change.reason,
    }



def _read_record_field(record: Any, field_name: str, default: Any = _MISSING) -> Any:
    if isinstance(record, Mapping):
        if default is _MISSING:
            return record[field_name]
        return record.get(field_name, default)
    if default is _MISSING:
        return getattr(record, field_name)
    return getattr(record, field_name, default)


class BankAccountStore:
    """Manage bank accounts and simple in-memory transactions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._accounts: dict[tuple[str, str], BankAccount] = {}
        self._account_keys_by_id: dict[str, tuple[str, str]] = {}
        self._transactions: dict[tuple[str, str], list[AccountTransaction]] = {}
        self._status_history: dict[tuple[str, str], list[AccountStatusChange]] = {}

    def _get_account_unlocked(self, account_key: tuple[str, str]) -> BankAccount:
        try:
            return self._accounts[account_key]
        except KeyError as exc:
            raise NotFoundError("account not found") from exc

    def _get_account_key_for_id_unlocked(self, account_id: str) -> tuple[str, str]:
        try:
            return self._account_keys_by_id[account_id]
        except KeyError as exc:
            raise NotFoundError("account not found") from exc

    def create_account(
        self,
        *,
        user_id: str,
        account_number: str,
        account_type: str,
        balance: Decimal | int | str = Decimal("0.00"),
        status: AccountStatus | str = AccountStatus.ACTIVE,
        data_source: DataSource | str = DataSource.MOCK,
        routing_number: str | None = None,
        bank_name: str | None = None,
        bank_address: str | None = None,
        city: str | None = None,
        state: str | None = None,
        postal_code: str | None = None,
        provider: str | None = None,
        provider_item_id: str | None = None,
        provider_account_id: str | None = None,
        access_token_reference: str | None = None,
        currency: str = "USD",
    ) -> BankAccount:
        user_id = _validate_required_string("user_id", user_id)
        account_number = _validate_account_number(account_number)
        account_type = _validate_account_type(account_type)
        account_key = (user_id, account_number)
        with self._lock:
            if account_key in self._accounts:
                raise ValidationError("account already exists for this user")

            bank_details = _normalize_bank_details(
                {
                    "routing_number": routing_number,
                    "bank_name": bank_name,
                    "bank_address": bank_address,
                    "city": city,
                    "state": state,
                    "postal_code": postal_code,
                }
            )
            live_metadata = _normalize_live_metadata(
                {
                    "provider": provider,
                    "provider_item_id": provider_item_id,
                    "provider_account_id": provider_account_id,
                    "access_token_reference": access_token_reference,
                }
            )
            timestamp = _utc_now()
            account = BankAccount(
                user_id=user_id,
                account_number=account_number,
                account_type=account_type,
                balance=_validate_money("balance", balance, allow_zero=True),
                status=_validate_status(status),
                data_source=_validate_data_source(data_source),
                currency=_validate_currency(currency),
                account_id=str(uuid4()),
                created_at=timestamp,
                updated_at=timestamp,
                **bank_details,
                **live_metadata,
            )
            _validate_live_requirements(account)
            self._accounts[account_key] = account
            self._account_keys_by_id[account.account_id] = account_key
            self._transactions[account_key] = []
            self._status_history[account_key] = []
            return account

    def create_account_from_mock_data(
        self,
        *,
        user_id: str,
        profile: BankProfile,
        account_type: str = "checking",
        status: AccountStatus | str = AccountStatus.ACTIVE,
        currency: str = "USD",
    ) -> BankAccount:
        return self.create_account(
            user_id=user_id,
            account_number=profile.account_number,
            account_type=account_type,
            balance=profile.balance_usd,
            status=status,
            data_source=DataSource.MOCK,
            routing_number=profile.routing_number,
            bank_name=profile.bank_name,
            bank_address=profile.bank_address,
            city=profile.city,
            state=profile.state,
            postal_code=profile.postal_code,
            currency=currency,
        )

    def create_account_from_live_data(
        self,
        *,
        user_id: str,
        live_account: Any,
        access_token_reference: str,
        status: AccountStatus | str = AccountStatus.ACTIVE,
        currency: str | None = None,
    ) -> BankAccount:
        return self.create_account(
            user_id=user_id,
            account_number=_read_record_field(live_account, "account_number"),
            account_type=_read_record_field(live_account, "account_type"),
            balance=_read_record_field(live_account, "balance"),
            status=status,
            data_source=DataSource.LIVE,
            routing_number=_read_record_field(live_account, "routing_number"),
            bank_name=_read_record_field(live_account, "bank_name", None),
            bank_address=_read_record_field(live_account, "bank_address", None),
            city=_read_record_field(live_account, "city", None),
            state=_read_record_field(live_account, "state", None),
            postal_code=_read_record_field(live_account, "postal_code", None),
            provider=_read_record_field(live_account, "provider", "plaid"),
            provider_item_id=_read_record_field(live_account, "item_id", None),
            provider_account_id=_read_record_field(live_account, "account_id", None),
            access_token_reference=access_token_reference,
            currency=currency or _read_record_field(live_account, "currency", "USD"),
        )

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

    def list_accounts_for_user(self, user_id: str) -> list[BankAccount]:
        return self.list_accounts(user_id)

    def get_account(self, user_id: str, account_number: str) -> BankAccount:
        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_account_number(account_number),
        )
        with self._lock:
            return self._get_account_unlocked(account_key)

    def get_account_by_id(self, account_id: str) -> BankAccount:
        normalized_account_id = _validate_required_string("account_id", account_id)
        with self._lock:
            account_key = self._get_account_key_for_id_unlocked(normalized_account_id)
            return self._get_account_unlocked(account_key)

    def get_owned_account_by_id(self, user_id: str, account_id: str) -> BankAccount:
        normalized_user_id = _validate_required_string("user_id", user_id)
        with self._lock:
            account = self.get_account_by_id(account_id)
            if account.user_id != normalized_user_id:
                raise NotFoundError("account not found")
            return account

    def update_account(self, user_id: str, account_number: str, **updates: Any) -> BankAccount:
        unknown_fields = set(updates) - set(_UPDATABLE_ACCOUNT_FIELDS)
        if unknown_fields:
            unknown = ", ".join(sorted(unknown_fields))
            raise ValidationError(f"unsupported update field(s): {unknown}")

        normalized_updates: dict[str, Any] = {}
        if "account_type" in updates:
            normalized_updates["account_type"] = _validate_account_type(updates["account_type"])
        if "status" in updates:
            normalized_updates["status"] = _validate_status(updates["status"])
        if "data_source" in updates:
            normalized_updates["data_source"] = _validate_data_source(updates["data_source"])
        if "currency" in updates:
            normalized_updates["currency"] = _validate_currency(updates["currency"])

        normalized_reason = None
        if "status_reason" in updates:
            normalized_reason = _validate_optional_string("status_reason", updates["status_reason"])
            if "status" not in updates:
                raise ValidationError("status_reason can only be provided with a status update")

        normalized_updates.update(_normalize_bank_details(updates))
        normalized_updates.update(_normalize_live_metadata(updates))

        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_account_number(account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            if "status" in normalized_updates:
                _validate_status_transition(account.status, normalized_updates["status"], normalized_reason)
            if not normalized_updates:
                raise ValidationError("at least one updatable field is required")
            normalized_updates["updated_at"] = _utc_now()
            updated_account = replace(account, **normalized_updates)
            _validate_live_requirements(updated_account)
            self._accounts[account_key] = updated_account
            if "status" in normalized_updates and normalized_updates["status"] != account.status:
                self._status_history[account_key].append(
                    AccountStatusChange(
                        account_id=account.account_id,
                        account_number=account.account_number,
                        old_status=account.status,
                        new_status=normalized_updates["status"],
                        changed_at=updated_account.updated_at or normalized_updates["updated_at"],
                        reason=normalized_reason,
                    )
                )
            return updated_account

    def update_account_by_id(self, account_id: str, **updates: Any) -> BankAccount:
        account = self.get_account_by_id(account_id)
        return BankAccountStore.update_account(self, account.user_id, account.account_number, **updates)

    def add_transaction(
        self,
        user_id: str,
        account_number: str,
        amount: Decimal | int | str,
        transaction_type: str,
        description: str | None = None,
    ) -> AccountTransaction:
        normalized_transaction_type = _validate_required_string("transaction_type", transaction_type).lower()
        if normalized_transaction_type not in {"deposit", "withdrawal"}:
            raise ValidationError("transaction_type must be deposit or withdrawal")

        amount_decimal = _validate_money("amount", amount, allow_zero=False)
        description_text = _validate_optional_string("description", description)
        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_account_number(account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            if account.status != AccountStatus.ACTIVE:
                raise ValidationError("account must be active to process transactions")
            new_balance = account.balance + amount_decimal
            if normalized_transaction_type == "withdrawal":
                new_balance = account.balance - amount_decimal
                if new_balance < Decimal("0.00"):
                    raise ValidationError("insufficient funds")

            timestamp = _utc_now()
            updated_account = replace(account, balance=new_balance, updated_at=timestamp)
            self._accounts[account_key] = updated_account

            transaction = AccountTransaction(
                transaction_id=str(uuid4()),
                account_id=updated_account.account_id,
                account_number=updated_account.account_number,
                amount=amount_decimal,
                transaction_type=normalized_transaction_type,
                created_at=timestamp,
                resulting_balance=updated_account.balance,
                description=description_text,
            )
            self._transactions[account_key].append(transaction)
            return transaction

    def add_transaction_by_id(
        self,
        account_id: str,
        *,
        amount: Decimal | int | str,
        transaction_type: str,
        description: str | None = None,
    ) -> AccountTransaction:
        account = self.get_account_by_id(account_id)
        return BankAccountStore.add_transaction(
            self,
            account.user_id,
            account.account_number,
            amount,
            transaction_type,
            description,
        )

    def list_transactions(self, user_id: str, account_number: str) -> list[AccountTransaction]:
        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_account_number(account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            return list(self._transactions[(account.user_id, account.account_number)])

    def list_transactions_by_id(self, account_id: str) -> list[AccountTransaction]:
        account = self.get_account_by_id(account_id)
        return BankAccountStore.list_transactions(self, account.user_id, account.account_number)

    def get_status_history(self, user_id: str, account_number: str) -> list[AccountStatusChange]:
        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_account_number(account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            return list(self._status_history[(account.user_id, account.account_number)])

    def get_status_history_by_id(self, account_id: str) -> list[AccountStatusChange]:
        account = self.get_account_by_id(account_id)
        return BankAccountStore.get_status_history(self, account.user_id, account.account_number)

    def get_balance_status(self, user_id: str, account_number: str) -> dict[str, str]:
        account = self.get_account(user_id, account_number)
        return {
            "account_id": account.account_id,
            "masked_account_number": account.masked_account_number,
            "balance": f"{account.balance:.2f}",
            "currency": account.currency,
            "status": account.status.value,
        }

    def get_balance_status_by_id(self, account_id: str) -> dict[str, str]:
        account = self.get_account_by_id(account_id)
        return BankAccountStore.get_balance_status(self, account.user_id, account.account_number)


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



def get_account_audit_history(
    store: BankAccountStore,
    user_id: str,
    account_number: str,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "transactions": [
            _serialize_transaction(transaction)
            for transaction in store.list_transactions(user_id, account_number)
        ],
        "status_changes": [
            _serialize_status_change(change)
            for change in store.get_status_history(user_id, account_number)
        ],
    }



def create_account_from_mock_data(
    store: BankAccountStore,
    *,
    user_id: str,
    profile: BankProfile,
    account_type: str = "checking",
    status: AccountStatus | str = AccountStatus.ACTIVE,
    currency: str = "USD",
) -> BankAccount:
    return store.create_account_from_mock_data(
        user_id=user_id,
        profile=profile,
        account_type=account_type,
        status=status,
        currency=currency,
    )



def create_account_from_live_api(
    store: BankAccountStore,
    *,
    user_id: str,
    live_account: Any,
    access_token_reference: str,
    status: AccountStatus | str = AccountStatus.ACTIVE,
    currency: str | None = None,
) -> BankAccount:
    return store.create_account_from_live_data(
        user_id=user_id,
        live_account=live_account,
        access_token_reference=access_token_reference,
        status=status,
        currency=currency,
    )



def handle_request(
    store: BankAccountStore,
    *,
    method: str,
    path: str,
    user_id: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    normalized_method = method.upper().strip()
    normalized_path = path.strip("/")
    parts = normalized_path.split("/") if normalized_path else []

    try:
        requester = _validate_required_string("user_id", user_id)
        if normalized_method == "GET" and len(parts) == 2 and parts[0] == "accounts":
            account = store.get_owned_account_by_id(requester, parts[1])
            return 200, {"account": _serialize_account(account)}

        if normalized_method == "PATCH" and len(parts) == 2 and parts[0] == "accounts":
            if payload is None:
                raise APIError(400, "payload is required")
            account = store.get_owned_account_by_id(requester, parts[1])
            updated = store.update_account(account.user_id, account.account_number, **payload)
            return 200, {"account": _serialize_account(updated)}

        if normalized_method == "GET" and len(parts) == 3 and parts[0] == "users" and parts[2] == "accounts":
            target_user_id = _validate_required_string("user_id", parts[1])
            if requester != target_user_id:
                raise NotFoundError("account not found")
            accounts = store.list_accounts(target_user_id)
            return 200, {"accounts": [_serialize_account(account) for account in accounts]}

        if normalized_method == "GET" and len(parts) == 3 and parts[0] == "accounts" and parts[2] == "balance":
            account = store.get_owned_account_by_id(requester, parts[1])
            return 200, store.get_balance_status(account.user_id, account.account_number)

        if normalized_method == "GET" and len(parts) == 3 and parts[0] == "accounts" and parts[2] == "status":
            account = store.get_owned_account_by_id(requester, parts[1])
            balance_status = store.get_balance_status(account.user_id, account.account_number)
            return 200, {
                "account_id": balance_status["account_id"],
                "masked_account_number": balance_status["masked_account_number"],
                "status": balance_status["status"],
            }

        raise APIError(404, "endpoint not found")
    except ValidationError as exc:
        return 400, {"error": str(exc)}
    except NotFoundError:
        return 404, {"error": "account not found"}
    except APIError as exc:
        return exc.status_code, {"error": exc.message}
