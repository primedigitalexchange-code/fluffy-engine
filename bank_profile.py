from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BankProfile:
    account_name: str
    account_number: str
    routing_number: str
    bank_name: str
    bank_address: str
    city: str
    state: str
    postal_code: str
    balance_usd: str


def _routing_checksum(digits: str) -> int:
    weights = (3, 7, 1) * 3
    total = sum(int(digit) * weight for digit, weight in zip(digits, weights))
    return (10 - (total % 10)) % 10


def build_bank_profile(account_name: str = "My Account") -> BankProfile:
    routing_prefix = "99000000"
    routing_number = f"{routing_prefix}{_routing_checksum(routing_prefix)}"
    balance = Decimal("2485.77")
    street_address = "742 Evergreen Terrace"
    city = "Springfield"
    state = "IL"
    postal_code = "62704"

    return BankProfile(
        account_name=account_name,
        account_number="000123456789",
        routing_number=routing_number,
        bank_name="Live Build Bank",
        bank_address=f"{street_address}, {city}, {state} {postal_code}, USA",
        city=city,
        state=state,
        postal_code=postal_code,
        balance_usd=f"{balance:.2f}",
    )


def main() -> None:
    print(json.dumps(asdict(build_bank_profile()), indent=2))


if __name__ == "__main__":
    main()
