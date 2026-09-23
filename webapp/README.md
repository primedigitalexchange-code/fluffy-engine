# fluffy-engine webapp demo

This `webapp/` package adds a thin FastAPI application layer on top of the existing
banking connector and in-memory account store.

## Install

```bash
pip install -r requirements-webapp.txt
```

## Required environment variables

Application auth and web settings:

```bash
export APP_USERNAME='demo-user'
export APP_PASSWORD='demo-password'
export APP_JWT_SECRET='replace-with-a-random-long-secret'
export APP_CORS_ORIGINS='http://localhost:8000,http://127.0.0.1:8000'
```

Existing Plaid/live banking settings:

```bash
export FLUFFY_ENGINE_BANK_DATA_MODE=live
export FLUFFY_ENGINE_BANK_PROVIDER=plaid
export PLAID_CLIENT_ID='your-plaid-client-id'
export PLAID_SECRET='your-plaid-secret'
export PLAID_ENV=sandbox
export BANKING_TOKEN_ENCRYPTION_KEY='your-fernet-or-derived-key'
```

Start with `PLAID_ENV=sandbox` for testing.

## Run

```bash
uvicorn webapp.app:app --reload --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000/> for the login page.

## Behavior and limitations

- `POST /auth/login` validates `APP_USERNAME` and `APP_PASSWORD`, issues a short-lived signed JWT using `APP_JWT_SECRET`, and stores it in an HttpOnly cookie.
- `POST /bank/link-token` and `POST /bank/exchange-token` are authenticated and use the existing `PlaidConnector` helpers.
- Connected accounts are persisted only in the in-memory `BankAccountStore` for the lifetime of the process.
- `GET /bank/accounts/{account_id}/transactions` attempts live Plaid transaction history for linked live accounts and falls back to in-memory transactions when no live account metadata is available.
- Plaid Link handles bank credentials in the browser; this app does not collect raw bank usernames or passwords.

## Non-production warning

This demo is intentionally **not production-ready**. Production deployment requires:

- persistent storage instead of in-memory state
- secure secret management
- HTTPS everywhere
- CSRF defenses and rate limiting at the web layer
- stronger user management than a single env-backed username/password
- operational monitoring and audit controls appropriate for financial data

## Tests

Run the webapp-focused tests:

```bash
python -m unittest tests.test_webapp -q
```

Then run the full suite:

```bash
python -m unittest discover -s tests -q
```
