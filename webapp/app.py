from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from bank_account import (
    BankAccount,
    BankAccountStore,
    NotFoundError,
    ValidationError,
    create_account_from_live_api,
    list_user_accounts,
)
from banking_connector import (
    AppCredentials,
    BankingAPIError,
    BankingAuthenticationError,
    BankingConfig,
    ConfigurationError,
    PlaidConnector,
    verify_app_login,
)

_ALGORITHM = "HS256"
_DEFAULT_AUTH_COOKIE_NAME = "fluffy_engine_auth"
_DEFAULT_TOKEN_TTL_MINUTES = 45
_DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000,http://127.0.0.1:8000"
)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class PublicTokenRequest(BaseModel):
    public_token: str = Field(..., min_length=1)


def _bool_from_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_cors_origins() -> list[str]:
    raw = os.getenv("APP_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _load_jwt_secret() -> str:
    secret = os.getenv("APP_JWT_SECRET")
    if secret is None or not secret.strip():
        raise ConfigurationError("APP_JWT_SECRET must be set to a non-empty value")
    return secret.strip()


def _token_expiry_minutes() -> int:
    raw = os.getenv("APP_JWT_EXPIRES_MINUTES", str(_DEFAULT_TOKEN_TTL_MINUTES))
    return max(1, int(raw))


def _mask_account_number(value: str) -> str:
    if len(value) <= 4:
        return f"***{value}"
    return f"{'*' * max(4, len(value) - 4)}{value[-4:]}"


def _serialize_user_account(account_payload: dict[str, Any]) -> dict[str, Any]:
    account_number = str(account_payload["account_number"])
    return {
        "account_id": account_payload.get("provider_account_id") or account_number,
        "masked_account_number": _mask_account_number(account_number),
        "bank_name": account_payload.get("bank_name"),
        "account_type": account_payload.get("account_type"),
        "balance": account_payload.get("balance"),
        "status": account_payload.get("status"),
        "data_source": account_payload.get("data_source"),
        "provider": account_payload.get("provider"),
    }


def _serialize_transaction(transaction: Any) -> dict[str, Any]:
    return {
        "transaction_id": transaction.transaction_id,
        "account_id": transaction.account_id,
        "name": transaction.name,
        "amount": f"{transaction.amount:.2f}",
        "posted_on": transaction.posted_on.isoformat(),
        "pending": transaction.pending,
        "iso_currency_code": transaction.iso_currency_code,
        "merchant_name": transaction.merchant_name,
    }


def create_app(
    *,
    account_store: BankAccountStore | None = None,
    connector_factory: Any | None = None,
) -> FastAPI:
    app = FastAPI(title="fluffy-engine webapp")
    auth_scheme = HTTPBearer(auto_error=False)
    cookie_name = os.getenv("APP_AUTH_COOKIE_NAME", _DEFAULT_AUTH_COOKIE_NAME).strip() or _DEFAULT_AUTH_COOKIE_NAME
    cookie_secure = _bool_from_env("APP_COOKIE_SECURE", True)
    cors_origins = _parse_cors_origins()
    jwt_ttl_minutes = _token_expiry_minutes()

    store = account_store or BankAccountStore()
    connector_cache: PlaidConnector | None = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_connector() -> PlaidConnector:
        nonlocal connector_cache
        if connector_factory is not None:
            return connector_factory()
        if connector_cache is None:
            connector_cache = PlaidConnector(BankingConfig.from_env())
        return connector_cache

    def create_access_token(username: str) -> tuple[str, datetime]:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=jwt_ttl_minutes)
        token = jwt.encode({"sub": username, "iat": int(now.timestamp()), "exp": int(expires_at.timestamp())}, _load_jwt_secret(), algorithm=_ALGORITHM)
        return token, expires_at

    def decode_access_token(token: str) -> str:
        try:
            payload = jwt.decode(token, _load_jwt_secret(), algorithms=[_ALGORITHM])
        except JWTError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token") from exc
        username = payload.get("sub")
        if not isinstance(username, str) or not username.strip():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")
        return username

    def get_current_user(
        request: Request,
        auth: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
    ) -> str:
        bearer_token = auth.credentials if auth is not None else None
        cookie_token = request.cookies.get(cookie_name)
        token = bearer_token or cookie_token
        if token is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        return decode_access_token(token)

    @app.exception_handler(ConfigurationError)
    async def handle_configuration_error(_: Request, exc: ConfigurationError) -> Response:
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def handle_validation_error(_: Request, exc: ValidationError) -> Response:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def handle_not_found_error(_: Request, exc: NotFoundError) -> Response:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(BankingAuthenticationError)
    async def handle_banking_auth_error(_: Request, exc: BankingAuthenticationError) -> Response:
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})

    @app.exception_handler(BankingAPIError)
    async def handle_banking_api_error(_: Request, exc: BankingAPIError) -> Response:
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})

    @app.post("/auth/login")
    def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
        credentials = AppCredentials.from_env()
        if not verify_app_login(payload.username, payload.password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
        token, expires_at = create_access_token(credentials.username)
        max_age = int((expires_at - datetime.now(timezone.utc)).total_seconds())
        response.set_cookie(
            key=cookie_name,
            value=token,
            max_age=max_age,
            expires=max_age,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )
        return {
            "username": credentials.username,
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expires_at.isoformat(),
        }

    @app.post("/auth/logout")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(key=cookie_name, path="/", samesite="lax")
        return {"status": "logged_out"}

    @app.get("/auth/me")
    def me(username: str = Depends(get_current_user)) -> dict[str, str]:
        return {"username": username}

    @app.post("/bank/link-token")
    def create_link_token(username: str = Depends(get_current_user)) -> dict[str, str]:
        connector = get_connector()
        return connector.create_link_token(user_id=username)

    @app.post("/bank/exchange-token")
    def exchange_token(payload: PublicTokenRequest, username: str = Depends(get_current_user)) -> dict[str, Any]:
        connector = get_connector()
        session = connector.connect_account(user_id=username, public_token=payload.public_token)
        created_accounts: list[BankAccount] = []
        for account in session.accounts:
            created_accounts.append(
                create_account_from_live_api(
                    store,
                    user_id=username,
                    live_account=account,
                    access_token_reference=session.access_token_reference,
                )
            )
        return {
            "item_id": session.item_id,
            "access_token_reference": session.access_token_reference,
            "accounts": [
                {
                    "account_id": account.provider_account_id or account.account_number,
                    "masked_account_number": _mask_account_number(account.account_number),
                    "bank_name": account.bank_name,
                    "account_type": account.account_type,
                    "balance": f"{account.balance:.2f}",
                    "status": account.status.value,
                }
                for account in created_accounts
            ],
        }

    @app.get("/bank/accounts")
    def list_accounts(username: str = Depends(get_current_user)) -> dict[str, list[dict[str, Any]]]:
        payload = list_user_accounts(store, username)
        return {"accounts": [_serialize_user_account(account) for account in payload["accounts"]]}

    def _resolve_user_account(username: str, account_id: str) -> dict[str, Any]:
        normalized = account_id.strip()
        if not normalized:
            raise ValidationError("account_id must be a non-empty string")
        payload = list_user_accounts(store, username)
        for account in payload["accounts"]:
            if account.get("provider_account_id") == normalized or account.get("account_number") == normalized:
                return account
        raise NotFoundError("account not found")

    @app.get("/bank/accounts/{account_id}/transactions")
    def get_account_transactions(account_id: str, username: str = Depends(get_current_user)) -> dict[str, Any]:
        account_payload = _resolve_user_account(username, account_id)
        item_id = account_payload.get("provider_item_id")
        provider_account_id = account_payload.get("provider_account_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValidationError("account is missing linked provider item information")
        connector = get_connector()
        transactions = connector.get_transaction_history(
            item_id,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today(),
            account_id=provider_account_id,
        )
        return {
            "account_id": account_payload.get("provider_account_id") or account_payload.get("account_number"),
            "transactions": [_serialize_transaction(transaction) for transaction in transactions],
        }

    static_dir = Path(__file__).resolve().parent / "static"

    @app.get("/")
    def login_page() -> FileResponse:
        return FileResponse(static_dir / "login.html")

    @app.get("/dashboard")
    def dashboard_page() -> FileResponse:
        return FileResponse(static_dir / "dashboard.html")

    return app


app = create_app()
