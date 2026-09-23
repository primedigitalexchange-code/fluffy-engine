# fluffy-engine

Minimal Python banking utilities with support for both synthetic test data and
live Plaid-backed account connectivity.

## Why Plaid

This repository uses **Plaid** for live banking support because it is the most
widely adopted aggregation option for US bank connectivity, offers broad
institution coverage, and exposes the specific Auth + Transactions products this
project needs for account/routing details, balances, and transaction history.

## Mock profile utility

Generate a synthetic US bank profile:

```bash
python bank_profile.py
```

## Bank account module

The in-memory `bank_account` module stores either mock or live account records.
Each `BankAccount` now includes a `data_source` field so callers can distinguish
between synthetic and production-linked accounts.

### Create an account from mock data

```python
from bank_account import BankAccountStore, create_account_from_mock_data
from bank_profile import build_bank_profile

store = BankAccountStore()
profile = build_bank_profile(account_name="Payroll")
account = create_account_from_mock_data(
    store,
    user_id="user-123",
    profile=profile,
    account_type="checking",
)

print(account.data_source.value)  # "mock"
```

### Create an account from live Plaid data

```python
from bank_account import BankAccountStore, create_account_from_live_api
from banking_connector import BankingConfig, PlaidConnector

config = BankingConfig.from_env()
connector = PlaidConnector(config)
session = connector.connect_account(
    user_id="user-123",
    public_token="public-token-from-plaid-link",
)

store = BankAccountStore()
linked_account = create_account_from_live_api(
    store,
    user_id="user-123",
    live_account=session.accounts[0],
    access_token_reference=session.access_token_reference,
)

print(linked_account.data_source.value)  # "live"
print(linked_account.bank_name)
```

## Live banking setup

Install the live-banking dependency first:

```bash
pip install cryptography
```

Then configure the environment:

```bash
export FLUFFY_ENGINE_BANK_DATA_MODE=live
export FLUFFY_ENGINE_BANK_PROVIDER=plaid
export PLAID_CLIENT_ID=your-plaid-client-id
export PLAID_SECRET=your-plaid-secret
export PLAID_ENV=sandbox
export BANKING_TOKEN_ENCRYPTION_KEY=$(python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
)
export BANKING_RATE_LIMIT_PER_MINUTE=30
export BANKING_LOG_LEVEL=INFO
```

If your application also needs its own runtime login separate from Plaid, provide
it through environment variables instead of hardcoding secrets in the repository:

```bash
export APP_USERNAME='your-app-username'
export APP_PASSWORD='your-app-password'
```

### Getting Plaid API credentials

1. Create a Plaid developer account.
2. Create an application in the Plaid dashboard.
3. Copy the `client_id` and `secret` for the environment you plan to use.
4. Start with `PLAID_ENV=sandbox` until your integration is fully validated.

### Connect a real bank account

Use Plaid Link on the client side, then exchange the returned `public_token`
server-side:

```python
from banking_connector import BankingConfig, PlaidConnector

connector = PlaidConnector(BankingConfig.from_env())
link_session = connector.create_link_token(user_id="user-123")
print(link_session["link_token"])

# After the frontend completes Plaid Link it should POST the public_token back.
connected = connector.connect_account(
    user_id="user-123",
    public_token="public-token-from-your-frontend",
)

print(connected.access_token_reference)
```

### Retrieve a live balance

```python
accounts = connector.get_account_information("plaid-item-id")
print(accounts[0].balance)
```

### Get transaction history

```python
from datetime import date

transactions = connector.get_transaction_history(
    "plaid-item-id",
    start_date=date(2026, 9, 1),
    end_date=date(2026, 9, 16),
)

for transaction in transactions:
    print(transaction.posted_on, transaction.name, transaction.amount)
```

## Security and compliance notes

- **No hardcoded banking credentials**: live credentials are read only from
  environment variables.
- **Encrypted token storage**: Plaid access tokens are encrypted in memory using
  `cryptography.fernet` before storage.
- **No credential handling in app code**: end users authenticate through Plaid
  Link, so this code never collects raw online-banking usernames or passwords.
- **Rate limiting**: outbound Plaid calls are throttled per configured
  `BANKING_RATE_LIMIT_PER_MINUTE`.
- **Audit logging**: connector logging records which Plaid endpoints were used
  without logging secrets or tokens.
- **Compliance**: if you persist live tokens or account metadata in production,
  move the encrypted store behind a KMS/HSM-backed secret vault and review your
  NACHA, PCI-DSS, and privacy obligations before launch.

## Limitations

- Plaid does not expose a traditional OAuth refresh token for Auth; the
  connector therefore rotates access tokens using
  `/item/access_token/invalidate`.
- Bank mailing addresses are not returned by Plaid Auth, so live records may
  not include `bank_address`, `city`, `state`, or `postal_code`.
- The included tests mock Plaid responses; live integration tests require your
  own Plaid credentials and are intentionally left commented out.