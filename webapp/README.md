# Webapp demo

This FastAPI dashboard is an **in-memory demo only**.

- Payments are store-local balance transfers between accounts already loaded into the process.
- The demo **does not** initiate ACH, wire, RTP, card, or any other live-money payment rail.
- Plaid is used only for linked-account metadata/balance retrieval in live mode; **Plaid alone does not execute payments in this repository**.
- The synthetic bank details in this repository are test fixtures only and must **never** be treated as real payment credentials.

## Run locally

Set application credentials first:

```bash
export APP_USERNAME='demo-user'
export APP_PASSWORD='demo-password'
```

Then run the app factory:

```bash
uvicorn webapp.app:create_demo_app --factory
```

Authenticate with the configured HTTP Basic credentials, then open `/dashboard`.
