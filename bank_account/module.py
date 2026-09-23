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


@dataclass(frozen=True)
class AccountTransaction:
    transaction_id: str
    account_number: str
    amount: Decimal
    transaction_type: str
    created_at: datetime


@dataclass(frozen=True)
class LiveTransfer:
    payment_id: str
    idempotency_key: str
    authorization_id: str | None
    transfer_id: str | None
    source_user_id: str
    source_account_number: str
    destination_user_id: str
    destination_account_number: str
    amount: Decimal
    ach_class: str
    status: str
    decision: str
    decision_rationale: str | None
    created_at: datetime
    last_updated_at: datetime
    network: str = "ach"


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


def _validate_data_source(value: DataSource | str) -> DataSource:
    if isinstance(value, DataSource):
        return value
    try:
        return DataSource(str(value).strip().lower())
    except ValueError as exc:
        raise ValidationError("data_source must be mock or live") from exc


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


def _validate_balance(value: Decimal | int | str) -> Decimal:
    if isinstance(value, float):
        raise ValidationError("money values must be provided as Decimal, int, or string")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("balance must be a valid decimal amount") from exc
    if not amount.is_finite():
        raise ValidationError("balance must be a finite decimal amount")
    quantized_amount = amount.quantize(Decimal("0.01"))
    if quantized_amount != amount:
        raise ValidationError("money values must have no more than 2 decimal places")
    amount = quantized_amount
    if amount < Decimal("0.00"):
        raise ValidationError("balance must be greater than or equal to 0")
    return amount


def _validate_live_requirements(account: BankAccount) -> None:
    if account.data_source != DataSource.LIVE:
        return
    if account.provider is None:
        raise ValidationError("provider is required when data_source is live")
    if account.access_token_reference is None:
        raise ValidationError("access_token_reference is required when data_source is live")


def _serialize_account(account: BankAccount) -> dict[str, Any]:
    return {
        "user_id": account.user_id,
        "account_number": account.account_number,
        "account_type": account.account_type,
        "balance": f"{account.balance:.2f}",
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
        self._transactions: dict[tuple[str, str], list[AccountTransaction]] = {}
        self._live_transfers: dict[str, LiveTransfer] = {}
        self._live_transfer_idempotency_index: dict[tuple[str, str], str] = {}
        self._live_transfer_provider_index: dict[str, str] = {}

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
    ) -> BankAccount:
        user_id = _validate_required_string("user_id", user_id)
        account_number = _validate_required_string("account_number", account_number)
        account_type = _validate_required_string("account_type", account_type)
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
            account = BankAccount(
                user_id=user_id,
                account_number=account_number,
                account_type=account_type,
                balance=_validate_balance(balance),
                status=_validate_status(status),
                data_source=_validate_data_source(data_source),
                **bank_details,
                **live_metadata,
            )
            _validate_live_requirements(account)
            self._accounts[account_key] = account
            self._transactions[account_key] = []
            return account

    def create_account_from_mock_data(
        self,
        *,
        user_id: str,
        profile: BankProfile,
        account_type: str = "checking",
        status: AccountStatus | str = AccountStatus.ACTIVE,
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
        )

    def create_account_from_live_data(
        self,
        *,
        user_id: str,
        live_account: Any,
        access_token_reference: str,
        status: AccountStatus | str = AccountStatus.ACTIVE,
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
            "data_source",
            *_BANK_DETAIL_FIELDS,
            *_LIVE_METADATA_FIELDS,
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
        if "data_source" in updates:
            normalized_updates["data_source"] = _validate_data_source(updates["data_source"])

        normalized_updates.update(_normalize_bank_details(updates))
        normalized_updates.update(_normalize_live_metadata(updates))

        account_key = (
            _validate_required_string("user_id", user_id),
            _validate_required_string("account_number", account_number),
        )
        with self._lock:
            account = self._get_account_unlocked(account_key)
            updated_account = replace(account, **normalized_updates)
            _validate_live_requirements(updated_account)
            self._accounts[account_key] = updated_account
            return updated_account

    def add_transaction(
        self,
        user_id: str,
        account_number: str,
        amount: Decimal | int | str,
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
            if account.status != AccountStatus.ACTIVE:
                raise ValidationError("account must be active to process transactions")
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

    def create_live_transfer(
        self,
        *,
        idempotency_key: str,
        source_user_id: str,
        source_account_number: str,
        destination_user_id: str,
        destination_account_number: str,
        amount: Decimal | int | str,
        ach_class: str,
        status: str,
        decision: str,
        decision_rationale: str | None,
        authorization_id: str | None = None,
        transfer_id: str | None = None,
        network: str = "ach",
        created_at: datetime | None = None,
    ) -> LiveTransfer:
        source_user_id = _validate_required_string("source_user_id", source_user_id)
        idempotency_key = _validate_required_string("idempotency_key", idempotency_key)
        source_account_number = _validate_required_string("source_account_number", source_account_number)
        destination_user_id = _validate_required_string("destination_user_id", destination_user_id)
        destination_account_number = _validate_required_string("destination_account_number", destination_account_number)
        normalized_status = _validate_required_string("status", status).lower()
        normalized_decision = _validate_required_string("decision", decision).lower()
        normalized_ach_class = _validate_required_string("ach_class", ach_class).lower()
        normalized_network = _validate_required_string("network", network).lower()
        normalized_rationale = (
            _validate_required_string("decision_rationale", decision_rationale)
            if decision_rationale is not None
            else None
        )
        normalized_authorization_id = (
            _validate_required_string("authorization_id", authorization_id)
            if authorization_id is not None
            else None
        )
        normalized_transfer_id = (
            _validate_required_string("transfer_id", transfer_id)
            if transfer_id is not None
            else None
        )
        amount_value = _validate_balance(amount)
        if amount_value <= Decimal("0.00"):
            raise ValidationError("amount must be greater than 0")
        now = created_at or datetime.now(timezone.utc)
        payment_id = str(uuid4())
        transfer = LiveTransfer(
            payment_id=payment_id,
            idempotency_key=idempotency_key,
            authorization_id=normalized_authorization_id,
            transfer_id=normalized_transfer_id,
            source_user_id=source_user_id,
            source_account_number=source_account_number,
            destination_user_id=destination_user_id,
            destination_account_number=destination_account_number,
            amount=amount_value,
            ach_class=normalized_ach_class,
            status=normalized_status,
            decision=normalized_decision,
            decision_rationale=normalized_rationale,
            created_at=now,
            last_updated_at=now,
            network=normalized_network,
        )
        with self._lock:
            index_key = (source_user_id, idempotency_key)
            if index_key in self._live_transfer_idempotency_index:
                raise ValidationError("live transfer already exists for this idempotency key")
            self._live_transfers[payment_id] = transfer
            self._live_transfer_idempotency_index[index_key] = payment_id
            if normalized_transfer_id is not None:
                self._live_transfer_provider_index[normalized_transfer_id] = payment_id
            return transfer

    def get_live_transfer(self, source_user_id: str, payment_id: str) -> LiveTransfer:
        source_user_id = _validate_required_string("source_user_id", source_user_id)
        payment_id = _validate_required_string("payment_id", payment_id)
        with self._lock:
            transfer = self._live_transfers.get(payment_id)
            if transfer is None or transfer.source_user_id != source_user_id:
                raise NotFoundError("live transfer not found")
            return transfer

    def get_live_transfer_by_idempotency_key(
        self, source_user_id: str, idempotency_key: str
    ) -> LiveTransfer | None:
        source_user_id = _validate_required_string("source_user_id", source_user_id)
        idempotency_key = _validate_required_string("idempotency_key", idempotency_key)
        with self._lock:
            payment_id = self._live_transfer_idempotency_index.get((source_user_id, idempotency_key))
            if payment_id is None:
                return None
            return self._live_transfers[payment_id]

    def update_live_transfer(
        self,
        payment_id: str,
        *,
        status: str | None = None,
        transfer_id: str | None = None,
        decision_rationale: str | None = None,
        network: str | None = None,
        updated_at: datetime | None = None,
    ) -> LiveTransfer:
        payment_id = _validate_required_string("payment_id", payment_id)
        with self._lock:
            transfer = self._live_transfers.get(payment_id)
            if transfer is None:
                raise NotFoundError("live transfer not found")
            updates: dict[str, Any] = {"last_updated_at": updated_at or datetime.now(timezone.utc)}
            if status is not None:
                updates["status"] = _validate_required_string("status", status).lower()
            if transfer_id is not None:
                normalized_transfer_id = _validate_required_string("transfer_id", transfer_id)
                updates["transfer_id"] = normalized_transfer_id
            if decision_rationale is not None:
                updates["decision_rationale"] = _validate_required_string("decision_rationale", decision_rationale)
            if network is not None:
                updates["network"] = _validate_required_string("network", network).lower()
            updated_transfer = replace(transfer, **updates)
            self._live_transfers[payment_id] = updated_transfer
            if updated_transfer.transfer_id:
                self._live_transfer_provider_index[updated_transfer.transfer_id] = payment_id
            return updated_transfer

    def update_live_transfer_status_by_transfer_id(
        self,
        transfer_id: str,
        *,
        status: str,
        decision_rationale: str | None = None,
        updated_at: datetime | None = None,
    ) -> LiveTransfer | None:
        transfer_id = _validate_required_string("transfer_id", transfer_id)
        with self._lock:
            payment_id = self._live_transfer_provider_index.get(transfer_id)
            if payment_id is None:
                return None
            transfer = self._live_transfers[payment_id]
            updates: dict[str, Any] = {
                "status": _validate_required_string("status", status).lower(),
                "last_updated_at": updated_at or datetime.now(timezone.utc),
            }
            if decision_rationale is not None:
                updates["decision_rationale"] = _validate_required_string("decision_rationale", decision_rationale)
            updated_transfer = replace(transfer, **updates)
            self._live_transfers[payment_id] = updated_transfer
            return updated_transfer


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


def create_account_from_mock_data(
    store: BankAccountStore,
    *,
    user_id: str,
    profile: BankProfile,
    account_type: str = "checking",
    status: AccountStatus | str = AccountStatus.ACTIVE,
) -> BankAccount:
    return store.create_account_from_mock_data(
        user_id=user_id,
        profile=profile,
        account_type=account_type,
        status=status,
    )


def create_account_from_live_api(
    store: BankAccountStore,
    *,
    user_id: str,
    live_account: Any,
    access_token_reference: str,
    status: AccountStatus | str = AccountStatus.ACTIVE,
) -> BankAccount:
    return store.create_account_from_live_data(
        user_id=user_id,
        live_account=live_account,
        access_token_reference=access_token_reference,
        status=status,
    )
