"""Data models for the bank account feature."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class AccountStatus(str, Enum):
    """Lifecycle states allowed for a bank account."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class BankAccount:
    """Canonical bank account record used by the bank account store."""

    account_id: str
    user_id: str
    account_number: str
    account_type: str
    balance: Decimal
    currency: str
    status: AccountStatus
    created_at: datetime
    updated_at: datetime

    @property
    def masked_account_number(self) -> str:
        """Return account number with all but last 4 characters masked."""

        suffix = self.account_number[-4:]
        return f"{'*' * max(len(self.account_number) - 4, 0)}{suffix}"


@dataclass(frozen=True)
class AccountTransaction:
    """Represents a single balance-affecting account transaction."""

    transaction_id: str
    account_id: str
    amount: Decimal
    transaction_type: str
    timestamp: datetime
    resulting_balance: Decimal
    description: str | None = None


@dataclass(frozen=True)
class AccountStatusChange:
    """Audit trail entry for a bank account status transition."""

    account_id: str
    old_status: AccountStatus
    new_status: AccountStatus
    changed_at: datetime
    reason: str | None


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamps for account records."""

    return datetime.now(timezone.utc)
