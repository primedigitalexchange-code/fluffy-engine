# fluffy-engine

Minimal Python utilities for synthetic bank data.

## Existing profile utility

Generate a mock US bank profile with account/routing details and balance:

```bash
python bank_profile.py
```

## Bank account module

The `bank_account` package provides in-memory account management with:

- bank account and transaction data models
- multiple accounts per user
- account status tracking (`active`, `inactive`, `suspended`)
- optional bank metadata (routing number, bank/address/location fields)
- validated create/retrieve/update/list operations
- API-style endpoint functions for retrieval, updates, account listing, and balance/status lookup

### Usage example

```python
from decimal import Decimal
from bank_account import BankAccount, BankAccountStore, list_user_accounts

store = BankAccountStore()
store.create_account(
    BankAccount(
        user_id="user-1",
        account_number="123456789012",
        account_type="checking",
        balance=Decimal("100.00"),
        routing_number="123456780",
        bank_name="Live Build Bank",
        state="IL",
    )
)

accounts = list_user_accounts(store, "user-1")
print(accounts)
```

### Configuration requirements

No external configuration is required. The module uses in-memory storage and is reset when the process exits.
