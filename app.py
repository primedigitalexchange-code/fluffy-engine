from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from bank_account import (
    BankAccountStore,
    DataSource,
    NotFoundError,
    ValidationError,
    create_account_from_live_api,
    get_account_balance_status,
    list_user_accounts,
    retrieve_account,
)
from banking_connector import (
    AppCredentials,
    BankingConnectorError,
    ConfigurationError,
    PlaidConnector,
    build_banking_connector_from_env,
    load_app_credentials_from_env,
)


security = HTTPBasic()


class PublicTokenRequest(BaseModel):
    public_token: str = Field(min_length=1)


class MockAccountRequest(BaseModel):
    account_number: str = Field(min_length=1)
    account_type: str = Field(min_length=1)
    balance: Decimal = Decimal("0.00")
    routing_number: str | None = None
    bank_name: str | None = None
    bank_address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class TransactionRequest(BaseModel):
    amount: Decimal
    transaction_type: str = Field(min_length=1)


class ApiUnavailableError(RuntimeError):
    pass


def _authenticated_user(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    configured = request.app.state.credentials
    if configured is None:
        try:
            configured = load_app_credentials_from_env()
        except ConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Application credentials are not configured",
            ) from exc

    username_matches = secrets.compare_digest(credentials.username, configured.username)
    password_matches = secrets.compare_digest(credentials.password, configured.password)
    if not (username_matches and password_matches):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid application credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return configured.username


def _account_or_404(store: BankAccountStore, user_id: str, account_number: str):
    try:
        return store.get_account(user_id, account_number)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc


def create_app(
    *,
    store: BankAccountStore | None = None,
    connector: PlaidConnector | None = None,
    credentials: AppCredentials | None = None,
) -> FastAPI:
    application = FastAPI(title="Fluffy Engine Banking API", version="1.0.0")
    application.state.store = store or BankAccountStore()
    application.state.connector = connector
    application.state.credentials = credentials

    @application.get("/healthz", status_code=status.HTTP_200_OK)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/accounts")
    def accounts(user_id: str = Depends(_authenticated_user)) -> dict[str, Any]:
        return list_user_accounts(application.state.store, user_id)

    @application.post("/accounts", status_code=status.HTTP_201_CREATED)
    def create_mock_account(
        payload: MockAccountRequest,
        user_id: str = Depends(_authenticated_user),
    ) -> dict[str, Any]:
        try:
            created = application.state.store.create_account(
                user_id=user_id,
                account_number=payload.account_number,
                account_type=payload.account_type,
                balance=payload.balance,
                routing_number=payload.routing_number,
                bank_name=payload.bank_name,
                bank_address=payload.bank_address,
                city=payload.city,
                state=payload.state,
                postal_code=payload.postal_code,
            )
        except ValidationError as exc:
            error_status = (
                status.HTTP_409_CONFLICT
                if str(exc) == "account already exists for this user"
                else status.HTTP_422_UNPROCESSABLE_ENTITY
            )
            raise HTTPException(status_code=error_status, detail=str(exc)) from exc
        return retrieve_account(application.state.store, user_id, created.account_number)

    @application.get("/accounts/{account_number}")
    def account(
        account_number: str,
        user_id: str = Depends(_authenticated_user),
    ) -> dict[str, Any]:
        try:
            return retrieve_account(application.state.store, user_id, account_number)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc

    @application.get("/accounts/{account_number}/balance")
    def balance(
        account_number: str,
        user_id: str = Depends(_authenticated_user),
    ) -> dict[str, str]:
        try:
            return get_account_balance_status(application.state.store, user_id, account_number)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc

    @application.post("/accounts/{account_number}/transactions", status_code=status.HTTP_201_CREATED)
    def create_transaction(
        account_number: str,
        payload: TransactionRequest,
        user_id: str = Depends(_authenticated_user),
    ) -> dict[str, Any]:
        account_record = _account_or_404(application.state.store, user_id, account_number)
        if account_record.data_source == DataSource.LIVE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Transactions on live accounts are managed by the banking provider",
            )
        try:
            transaction = application.state.store.add_transaction(
                user_id,
                account_number,
                payload.amount,
                payload.transaction_type,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc
        except ValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return {
            "transaction": {
                "transaction_id": transaction.transaction_id,
                "account_number": transaction.account_number,
                "amount": f"{transaction.amount:.2f}",
                "transaction_type": transaction.transaction_type,
                "created_at": transaction.created_at.isoformat(),
            }
        }

    @application.get("/accounts/{account_number}/transactions")
    def transactions(
        account_number: str,
        start_date: date | None = None,
        end_date: date | None = None,
        user_id: str = Depends(_authenticated_user),
    ) -> dict[str, list[dict[str, Any]]]:
        account_record = _account_or_404(application.state.store, user_id, account_number)
        if account_record.data_source == DataSource.LIVE:
            if start_date is None or end_date is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="start_date and end_date are required for live transactions",
                )
            if start_date > end_date:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="start_date must be on or before end_date",
                )
            try:
                results = _connector_from_application(application).get_transaction_history(
                    account_record.provider_item_id or "",
                    start_date=start_date,
                    end_date=end_date,
                    account_id=account_record.provider_account_id,
                )
            except ApiUnavailableError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Live banking is not configured",
                ) from exc
            except BankingConnectorError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Unable to retrieve transactions from the banking provider",
                ) from exc
            return {
                "transactions": [
                    {
                        "transaction_id": item.transaction_id,
                        "account_id": item.account_id,
                        "amount": f"{item.amount:.2f}",
                        "name": item.name,
                        "posted_on": item.posted_on.isoformat(),
                        "pending": item.pending,
                        "iso_currency_code": item.iso_currency_code,
                        "merchant_name": item.merchant_name,
                    }
                    for item in results
                ]
            }

        local_transactions = application.state.store.list_transactions(user_id, account_number)
        if start_date is not None:
            local_transactions = [
                item for item in local_transactions if item.created_at.date() >= start_date
            ]
        if end_date is not None:
            local_transactions = [
                item for item in local_transactions if item.created_at.date() <= end_date
            ]
        return {
            "transactions": [
                {
                    "transaction_id": item.transaction_id,
                    "amount": f"{item.amount:.2f}",
                    "transaction_type": item.transaction_type,
                    "created_at": item.created_at.isoformat(),
                }
                for item in local_transactions
            ]
        }

    @application.post("/banking/link-token")
    def create_link_token(user_id: str = Depends(_authenticated_user)) -> dict[str, str]:
        try:
            return _connector_from_application(application).create_link_token(user_id=user_id)
        except BankingConnectorError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to create a banking link token",
            ) from exc
        except ApiUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Live banking is not configured",
            ) from exc

    @application.post("/banking/accounts", status_code=status.HTTP_201_CREATED)
    def connect_accounts(
        payload: PublicTokenRequest,
        user_id: str = Depends(_authenticated_user),
    ) -> dict[str, list[dict[str, Any]]]:
        try:
            session = _connector_from_application(application).connect_account(
                user_id=user_id,
                public_token=payload.public_token,
            )
        except BankingConnectorError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to connect the banking account",
            ) from exc
        except ApiUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Live banking is not configured",
            ) from exc

        linked_accounts = []
        try:
            for live_account in session.accounts:
                saved = create_account_from_live_api(
                    application.state.store,
                    user_id=user_id,
                    live_account=live_account,
                    access_token_reference=session.access_token_reference,
                )
                linked_accounts.append(
                    retrieve_account(application.state.store, user_id, saved.account_number)["account"]
                )
        except ValidationError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {"accounts": linked_accounts}

    return application


def _connector_from_application(application: FastAPI) -> Any:
    connector = application.state.connector
    if connector is not None:
        return connector
    try:
        connector = build_banking_connector_from_env()
    except (ConfigurationError, ValueError) as exc:
        raise ApiUnavailableError from exc
    application.state.connector = connector
    return connector


app = create_app()
