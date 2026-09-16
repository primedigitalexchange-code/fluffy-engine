# fluffy-engine

Minimal Python utility for generating a mock US bank profile with:

- account number
- routing number
- bank name
- bank address
- current balance
- USA postal code

All values are synthetic example data.

## Usage

```bash
python bank_profile.py
```

## Bank account module

The repository also includes an in-memory `bank_account` module for storing
account records with optional bank profile details.

```python
from bank_account import BankAccountStore

store = BankAccountStore()
account = store.create_account(
    user_id="user-123",
    account_number="000123456789",
    account_type="checking",
    balance="2485.77",
    routing_number="990000000",
    bank_name="Live Build Bank",
    bank_address="742 Evergreen Terrace, Springfield, IL 62704, USA",
    city="Springfield",
    state="IL",
    postal_code="62704",
)

print(account.bank_name)
```