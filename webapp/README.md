# fluffy-engine webapp

FastAPI + static HTML/JS dashboard layer built on top of:

- `AppCredentials` / `verify_app_login` style app auth in `banking_connector.py`
- `BankingConfig` / `PlaidConnector` for live Plaid operations
- `EncryptedTokenStore` via `PlaidConnector`
- `bank_account` account storage/serialization helpers

## Required environment variables

Existing variables from the core library:

- `APP_USERNAME`
- `APP_PASSWORD`
- `FLUFFY_ENGINE_BANK_DATA_MODE=live`
- `FLUFFY_ENGINE_BANK_PROVIDER=plaid`
- `PLAID_CLIENT_ID`
- `PLAID_SECRET`
- `PLAID_ENV` (`sandbox`, `development`, or `production`)
- `BANKING_TOKEN_ENCRYPTION_KEY`

Webapp-specific variables:

- `APP_JWT_SECRET` (required, non-empty)
- `APP_CORS_ORIGINS` (optional, comma-separated origins; defaults to localhost dev origins)
- `APP_JWT_EXPIRES_MINUTES` (optional, default `45`)
- `APP_AUTH_COOKIE_NAME` (optional, default `fluffy_engine_auth`)
- `APP_COOKIE_SECURE` (optional, default `true`; set `false` for local HTTP-only development)

## Install dependencies

```bash
pip install -r requirements-webapp.txt
```

## Run the backend

```bash
uvicorn webapp.app:app --reload --host 0.0.0.0 --port 8000
```

Open:

- Login page: `http://localhost:8000/`
- Dashboard: `http://localhost:8000/dashboard`

## Plaid sandbox end-to-end flow

1. Log in with `APP_USERNAME` / `APP_PASSWORD`.
2. Click **Connect a bank account**.
3. In Plaid Link sandbox, choose a test institution such as `First Platypus Bank`.
4. Use sandbox credentials from Plaid docs (for example `user_good` / `pass_good`).
5. On successful Link callback, the dashboard exchanges the `public_token`, stores linked accounts, and displays accounts + transactions.

## Tests

Run webapp tests:

```bash
python -m unittest tests.test_webapp -q
```

Run full suite:

```bash
python -m unittest discover -s tests -q
```

## Security caveats

- Account storage is in-memory and not persistent; this is not production-ready state management.
- JWT signing relies on `APP_JWT_SECRET`; use a strong secret and manage/rotate it with a proper secrets manager in production.
- The app does not collect raw online banking credentials directly; Plaid Link handles user bank credential entry.
