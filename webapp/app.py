from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
import os
from typing import Any

import jwt
from jwt import InvalidTokenError
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bank_account import (
    AccountTransaction,
    BankAccount,
    BankAccountStore,
    DataSource,
    NotFoundError,
    ValidationError as AccountValidationError,
    create_account_from_live_api,
)
from banking_connector import (
    BankingAPIError,
    BankingAuthenticationError,
    BankTransaction,
    ConfigurationError,
    TokenStorageError,
    build_banking_connector_from_env,
    verify_app_login,
)

AUTH_COOKIE_NAME = "fluffy_engine_auth"
JWT_ALGORITHM = "HS256"
JWT_LIFETIME_SECONDS = 15 * 60
DEFAULT_CORS_ORIGINS = (
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
http_bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenExchangeRequest(BaseModel):
    public_token: str


class AppState:
    def __init__(
        self,
        *,
        env: Mapping[str, str],
        account_store: BankAccountStore,
        connector_factory: Callable[[], Any],
        clock: Callable[[], datetime],
    ) -> None:
        self.env = env
        self.account_store = account_store
        self.connector_factory = connector_factory
        self.clock = clock


def create_app(
    *,
    env: Mapping[str, str] | None = None,
    account_store: BankAccountStore | None = None,
    connector_factory: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    env_source: Mapping[str, str] = os.environ if env is None else env
    store = account_store or BankAccountStore()
    app_clock = clock or (lambda: datetime.now(UTC))
    connector_builder = connector_factory or (lambda: build_banking_connector_from_env(env_source))

    app = FastAPI(title="fluffy-engine live bank dashboard")
    app.state.webapp = AppState(
        env=env_source,
        account_store=store,
        connector_factory=connector_builder,
        clock=app_clock,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_cors_origins(env_source.get("APP_CORS_ORIGINS")),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(ConfigurationError)
    async def handle_configuration_error(_: Request, exc: ConfigurationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
        )

    @app.exception_handler(AccountValidationError)
    async def handle_validation_error(_: Request, exc: AccountValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @app.exception_handler(NotFoundError)
    async def handle_not_found(_: Request, __: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "Resource not found"},
        )

    @app.exception_handler(BankingAuthenticationError)
    async def handle_banking_auth(_: Request, exc: BankingAuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc)},
        )

    @app.exception_handler(BankingAPIError)
    async def handle_banking_api(_: Request, exc: BankingAPIError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc)},
        )

    @app.exception_handler(TokenStorageError)
    async def handle_token_storage(_: Request, exc: TokenStorageError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": str(exc)},
        )

    @app.get("/", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.post("/auth/login")
    async def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
        if not verify_app_login(payload.username, payload.password, env=env_source):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )

        token = _create_auth_token(payload.username, env_source, app_clock)
        response.set_cookie(
            AUTH_COOKIE_NAME,
            token,
            httponly=True,
            max_age=JWT_LIFETIME_SECONDS,
            samesite="lax",
            secure=False,
            path="/",
        )
        return {
            "username": payload.username,
            "access_token": token,
            "token_type": "bearer",
            "expires_in": JWT_LIFETIME_SECONDS,
        }

    @app.post("/auth/logout")
    async def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(AUTH_COOKIE_NAME, path="/")
        return {"status": "logged_out"}

    @app.get("/auth/me")
    async def me(username: str = Depends(_build_current_user_dependency(app))) -> dict[str, str]:
        return {"username": username}

    @app.post("/bank/link-token")
    async def create_link_token(username: str = Depends(_build_current_user_dependency(app))) -> dict[str, Any]:
        connector = app.state.webapp.connector_factory()
        return connector.create_link_token(user_id=username)

    @app.post("/bank/exchange-token")
    async def exchange_token(
        payload: TokenExchangeRequest,
        username: str = Depends(_build_current_user_dependency(app)),
    ) -> dict[str, Any]:
        connector = app.state.webapp.connector_factory()
        session = connector.connect_account(user_id=username, public_token=payload.public_token)
        persisted_accounts: list[dict[str, Any]] = []
        for live_account in session.accounts:
            try:
                account = create_account_from_live_api(
                    app.state.webapp.account_store,
                    user_id=username,
                    live_account=live_account,
                    access_token_reference=session.access_token_reference,
                )
            except AccountValidationError as exc:
                if str(exc) != "account already exists for this user":
                    raise
                account = app.state.webapp.account_store.get_account(username, live_account.account_number)
            persisted_accounts.append(_serialize_safe_account(account))
        return {
            "item_id": session.item_id,
            "accounts": persisted_accounts,
        }

    @app.get("/bank/accounts")
    async def list_accounts(username: str = Depends(_build_current_user_dependency(app))) -> dict[str, Any]:
        accounts = app.state.webapp.account_store.list_accounts(username)
        return {"accounts": [_serialize_safe_account(account) for account in accounts]}

    @app.get("/bank/accounts/{account_id}/transactions")
    async def list_transactions(
        account_id: str,
        username: str = Depends(_build_current_user_dependency(app)),
    ) -> dict[str, Any]:
        account = _find_owned_account(app.state.webapp.account_store, username, account_id)
        source = "in-memory"
        transactions: list[dict[str, Any]]
        if (
            account.data_source == DataSource.LIVE
            and account.access_token_reference
            and account.provider_account_id
        ):
            connector = app.state.webapp.connector_factory()
            end_date = app.state.webapp.clock().date()
            start_date = end_date - timedelta(days=30)
            plaid_transactions = connector.get_transaction_history(
                account.access_token_reference,
                start_date=start_date,
                end_date=end_date,
                account_id=account.provider_account_id,
            )
            transactions = [_serialize_plaid_transaction(transaction) for transaction in plaid_transactions]
            source = "plaid"
        else:
            store_transactions = app.state.webapp.account_store.list_transactions(username, account.account_number)
            transactions = [_serialize_store_transaction(transaction) for transaction in store_transactions]
        return {
            "account_id": _public_account_id(account),
            "source": source,
            "transactions": transactions,
        }

    return app

def _build_current_user_dependency(app: FastAPI) -> Callable[..., str]:
    async def get_current_user(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
    ) -> str:
        token = None
        if credentials is not None:
            token = credentials.credentials
        else:
            token = request.cookies.get(AUTH_COOKIE_NAME)
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        try:
            return _decode_auth_token(token, app.state.webapp.env)
        except ConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except InvalidTokenError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc

    return get_current_user


def _parse_cors_origins(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _require_jwt_secret(env: Mapping[str, str]) -> str:
    secret = env.get("APP_JWT_SECRET", "")
    if not secret.strip():
        raise ConfigurationError("APP_JWT_SECRET must be configured for web authentication")
    return secret


def _create_auth_token(
    username: str,
    env: Mapping[str, str],
    clock: Callable[[], datetime],
) -> str:
    issued_at = clock()
    payload = {
        "sub": username,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(seconds=JWT_LIFETIME_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, _require_jwt_secret(env), algorithm=JWT_ALGORITHM)


def _decode_auth_token(token: str, env: Mapping[str, str]) -> str:
    payload = jwt.decode(token, _require_jwt_secret(env), algorithms=[JWT_ALGORITHM])
    username = payload.get("sub")
    if not isinstance(username, str) or not username.strip():
        raise InvalidTokenError("Missing token subject")
    return username


def _mask_account_number(account_number: str) -> str:
    last_four = account_number[-4:]
    return f"****{last_four}"


def _public_account_id(account: BankAccount) -> str:
    return account.provider_account_id or account.account_number


def _serialize_safe_account(account: BankAccount) -> dict[str, Any]:
    return {
        "account_id": _public_account_id(account),
        "masked_account_number": _mask_account_number(account.account_number),
        "bank_name": account.bank_name,
        "account_type": account.account_type,
        "balance": f"{account.balance:.2f}",
        "status": account.status.value,
        "data_source": account.data_source.value,
    }


def _serialize_plaid_transaction(transaction: BankTransaction) -> dict[str, Any]:
    return {
        "transaction_id": transaction.transaction_id,
        "amount": f"{transaction.amount:.2f}",
        "name": transaction.name,
        "posted_on": transaction.posted_on.isoformat(),
        "pending": transaction.pending,
        "iso_currency_code": transaction.iso_currency_code,
        "merchant_name": transaction.merchant_name,
    }


def _serialize_store_transaction(transaction: AccountTransaction) -> dict[str, Any]:
    return {
        "transaction_id": transaction.transaction_id,
        "amount": f"{transaction.amount:.2f}",
        "name": transaction.transaction_type.title(),
        "posted_on": transaction.created_at.date().isoformat(),
        "pending": False,
        "transaction_type": transaction.transaction_type,
    }


def _find_owned_account(store: BankAccountStore, username: str, account_id: str) -> BankAccount:
    for account in store.list_accounts(username):
        if _public_account_id(account) == account_id:
            return account
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")


app = create_app()
