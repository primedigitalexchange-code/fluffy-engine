from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
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
    LiveTransfer,
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


class TransferCreateRequest(BaseModel):
    source_account_id: str
    destination_user_id: str
    destination_account_id: str
    amount: str
    ach_class: str
    idempotency_key: str
    memo: str | None = None


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
        self.webhook_key_cache: dict[str, tuple[dict[str, Any], datetime]] = {}
        self.transfer_status_stale_after_seconds = _transfer_status_stale_after_seconds(env)


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

    @app.post("/bank/transfers")
    async def create_transfer(
        payload: TransferCreateRequest,
        username: str = Depends(_build_current_user_dependency(app)),
    ) -> dict[str, Any]:
        existing = app.state.webapp.account_store.get_live_transfer_by_idempotency_key(
            username, payload.idempotency_key
        )
        if existing is not None:
            return {"transfer": _serialize_live_transfer(existing), "idempotent_replay": True}

        source_account = _find_owned_account(app.state.webapp.account_store, username, payload.source_account_id)
        destination_account = app.state.webapp.account_store.get_account(
            payload.destination_user_id,
            _resolve_account_number(
                app.state.webapp.account_store,
                payload.destination_user_id,
                payload.destination_account_id,
            ),
        )
        if (
            source_account.user_id == destination_account.user_id
            and source_account.account_number == destination_account.account_number
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source and destination accounts must differ")
        amount = _parse_amount(payload.amount)

        if source_account.data_source != DataSource.LIVE:
            withdrawal = app.state.webapp.account_store.add_transaction(
                source_account.user_id, source_account.account_number, amount, "withdrawal"
            )
            app.state.webapp.account_store.add_transaction(
                destination_account.user_id, destination_account.account_number, amount, "deposit"
            )
            return {
                "transfer": {
                    "payment_id": withdrawal.transaction_id,
                    "status": "completed",
                    "source_account": _mask_account_number(source_account.account_number),
                    "destination_account": _mask_account_number(destination_account.account_number),
                    "amount": f"{amount:.2f}",
                    "data_source": "in-memory",
                }
            }

        if not _live_transfers_enabled(env_source):
            raise ConfigurationError("Live transfers are disabled; set FLUFFY_ENGINE_ENABLE_LIVE_TRANSFERS=true")
        _require_live_transfer_config(env_source)
        if not source_account.access_token_reference or not source_account.provider_account_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source account is missing live provider metadata")

        connector = app.state.webapp.connector_factory()
        authorization = connector.create_transfer_authorization(
            access_token_reference=source_account.access_token_reference,
            account_id=source_account.provider_account_id,
            amount=amount,
            ach_class=payload.ach_class,
            user_legal_name=username,
        )
        decision_status = _authorization_status(authorization.decision)
        if authorization.decision != "approved":
            transfer = app.state.webapp.account_store.create_live_transfer(
                idempotency_key=payload.idempotency_key,
                source_user_id=source_account.user_id,
                source_account_number=source_account.account_number,
                destination_user_id=destination_account.user_id,
                destination_account_number=destination_account.account_number,
                amount=amount,
                ach_class=payload.ach_class,
                status=decision_status,
                decision=authorization.decision,
                decision_rationale=authorization.decision_rationale,
                authorization_id=authorization.authorization_id,
            )
            return {"transfer": _serialize_live_transfer(transfer)}

        transfer_result = connector.create_transfer(
            access_token_reference=source_account.access_token_reference,
            account_id=source_account.provider_account_id,
            authorization_id=authorization.authorization_id,
            amount=amount,
            ach_class=payload.ach_class,
            description=payload.memo or "fluffy-engine transfer",
            user_legal_name=username,
            idempotency_key=payload.idempotency_key,
        )
        transfer = app.state.webapp.account_store.create_live_transfer(
            idempotency_key=payload.idempotency_key,
            source_user_id=source_account.user_id,
            source_account_number=source_account.account_number,
            destination_user_id=destination_account.user_id,
            destination_account_number=destination_account.account_number,
            amount=amount,
            ach_class=payload.ach_class,
            status=transfer_result.status,
            decision=authorization.decision,
            decision_rationale=authorization.decision_rationale,
            authorization_id=authorization.authorization_id,
            transfer_id=transfer_result.transfer_id,
            network=transfer_result.network,
        )
        return {"transfer": _serialize_live_transfer(transfer)}

    @app.get("/bank/transfers/{payment_id}")
    async def get_transfer_status(
        payment_id: str,
        username: str = Depends(_build_current_user_dependency(app)),
    ) -> dict[str, Any]:
        transfer = app.state.webapp.account_store.get_live_transfer(username, payment_id)
        if (
            transfer.transfer_id
            and transfer.status in {"pending", "authorized"}
            and _is_transfer_stale(transfer, app.state.webapp.clock(), app.state.webapp.transfer_status_stale_after_seconds)
            and _live_transfers_enabled(env_source)
        ):
            _require_live_transfer_config(env_source)
            connector = app.state.webapp.connector_factory()
            remote_transfer = connector.get_transfer_status(transfer.transfer_id)
            transfer = app.state.webapp.account_store.update_live_transfer(
                transfer.payment_id,
                status=remote_transfer.status,
                network=remote_transfer.network,
                updated_at=app.state.webapp.clock(),
            )
        return {"transfer": _serialize_live_transfer(transfer)}

    @app.post("/webhooks/plaid/transfer")
    async def plaid_transfer_webhook(request: Request) -> dict[str, str]:
        if not _live_transfers_enabled(env_source):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        _require_live_transfer_config(env_source)
        payload_bytes = await request.body()
        signature = request.headers.get("Plaid-Verification")
        if not signature:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing webhook signature")
        claims = _verify_plaid_webhook_jwt(app, signature, payload_bytes)
        if claims.get("request_body_sha256") != hashlib.sha256(payload_bytes).hexdigest():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook payload hash mismatch")
        try:
            payload = await request.json()
        except Exception as exc:  # pragma: no cover - safety guard
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload") from exc
        transfer_id = _extract_transfer_id(payload)
        transfer_status = _extract_transfer_status(payload)
        if transfer_id is None or transfer_status is None:
            return {"status": "ignored"}
        updated = app.state.webapp.account_store.update_live_transfer_status_by_transfer_id(
            transfer_id,
            status=transfer_status,
            updated_at=app.state.webapp.clock(),
        )
        if updated is None:
            return {"status": "ignored"}
        return {"status": "processed"}

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


def _parse_amount(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="amount must be a valid decimal") from exc
    if amount <= Decimal("0.00"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="amount must be greater than 0")
    quantized = amount.quantize(Decimal("0.01"))
    if quantized != amount:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="amount must have no more than 2 decimal places")
    return quantized


def _public_account_id(account: BankAccount) -> str:
    return account.provider_account_id or account.account_number


def _resolve_account_number(store: BankAccountStore, user_id: str, account_id: str) -> str:
    for account in store.list_accounts(user_id):
        if _public_account_id(account) == account_id:
            return account.account_number
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Destination account not found")


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


def _serialize_live_transfer(transfer: LiveTransfer) -> dict[str, Any]:
    return {
        "payment_id": transfer.payment_id,
        "authorization_id": transfer.authorization_id,
        "transfer_id": transfer.transfer_id,
        "source_user_id": transfer.source_user_id,
        "source_account": _mask_account_number(transfer.source_account_number),
        "destination_user_id": transfer.destination_user_id,
        "destination_account": _mask_account_number(transfer.destination_account_number),
        "amount": f"{transfer.amount:.2f}",
        "ach_class": transfer.ach_class,
        "status": transfer.status,
        "decision": transfer.decision,
        "decision_rationale": transfer.decision_rationale,
        "network": transfer.network,
        "created_at": transfer.created_at.isoformat(),
        "last_updated_at": transfer.last_updated_at.isoformat(),
        "data_source": "plaid-transfer",
    }


def _authorization_status(decision: str) -> str:
    normalized = decision.strip().lower()
    if normalized == "approved":
        return "authorized"
    if normalized == "declined":
        return "declined"
    return "review_required"


def _live_transfers_enabled(env: Mapping[str, str]) -> bool:
    value = env.get("FLUFFY_ENGINE_ENABLE_LIVE_TRANSFERS", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _require_live_transfer_config(env: Mapping[str, str]) -> None:
    if not _live_transfers_enabled(env):
        return
    missing: list[str] = []
    if env.get("FLUFFY_ENGINE_BANK_DATA_MODE", "").strip().lower() != "live":
        missing.append("FLUFFY_ENGINE_BANK_DATA_MODE=live")
    if not env.get("PLAID_WEBHOOK_URL", "").strip():
        missing.append("PLAID_WEBHOOK_URL")
    if not env.get("PLAID_WEBHOOK_AUDIENCE", "").strip():
        missing.append("PLAID_WEBHOOK_AUDIENCE")
    if missing:
        raise ConfigurationError(
            f"Live transfers require the following configuration: {', '.join(missing)}"
        )


def _transfer_status_stale_after_seconds(env: Mapping[str, str]) -> int:
    raw_value = env.get("FLUFFY_ENGINE_TRANSFER_STATUS_STALE_SECONDS", "300")
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError("FLUFFY_ENGINE_TRANSFER_STATUS_STALE_SECONDS must be an integer") from exc
    if parsed <= 0:
        raise ConfigurationError("FLUFFY_ENGINE_TRANSFER_STATUS_STALE_SECONDS must be greater than 0")
    return parsed


def _is_transfer_stale(transfer: LiveTransfer, now: datetime, stale_after_seconds: int) -> bool:
    return transfer.last_updated_at + timedelta(seconds=stale_after_seconds) <= now


def _verify_plaid_webhook_jwt(app: FastAPI, signed_jwt: str, payload_bytes: bytes) -> dict[str, Any]:
    del payload_bytes
    try:
        header = jwt.get_unverified_header(signed_jwt)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature") from exc
    key_id = header.get("kid")
    if not isinstance(key_id, str) or not key_id.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing webhook key id")
    key = _load_webhook_verification_key(app, key_id.strip())
    try:
        public_key = jwt.PyJWK.from_dict(key).key
        return jwt.decode(
            signed_jwt,
            key=public_key,
            algorithms=[header.get("alg", "ES256")],
            audience=app.state.webapp.env.get("PLAID_WEBHOOK_AUDIENCE"),
        )
    except InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature") from exc


def _load_webhook_verification_key(app: FastAPI, key_id: str) -> dict[str, Any]:
    cached = app.state.webapp.webhook_key_cache.get(key_id)
    now = app.state.webapp.clock()
    if cached is not None:
        key, expiration = cached
        if expiration > now:
            return key
    connector = app.state.webapp.connector_factory()
    key = connector.get_webhook_verification_key(key_id)
    app.state.webapp.webhook_key_cache[key_id] = (key, now + timedelta(hours=1))
    return key


def _extract_transfer_id(payload: Mapping[str, Any]) -> str | None:
    if isinstance(payload.get("transfer"), Mapping):
        transfer = payload["transfer"]
        transfer_id = transfer.get("id")
        if isinstance(transfer_id, str) and transfer_id.strip():
            return transfer_id.strip()
    if isinstance(payload.get("event"), Mapping):
        event = payload["event"]
        transfer_id = event.get("transfer_id")
        if isinstance(transfer_id, str) and transfer_id.strip():
            return transfer_id.strip()
    transfer_id = payload.get("transfer_id")
    if isinstance(transfer_id, str) and transfer_id.strip():
        return transfer_id.strip()
    return None


def _extract_transfer_status(payload: Mapping[str, Any]) -> str | None:
    if isinstance(payload.get("transfer"), Mapping):
        transfer = payload["transfer"]
        status_value = transfer.get("status")
        if isinstance(status_value, str) and status_value.strip():
            return _normalize_webhook_transfer_status(status_value)
    if isinstance(payload.get("event"), Mapping):
        event = payload["event"]
        event_type = event.get("event_type")
        if isinstance(event_type, str) and event_type.strip():
            return _normalize_webhook_transfer_status(event_type)
    status_value = payload.get("status")
    if isinstance(status_value, str) and status_value.strip():
        return _normalize_webhook_transfer_status(status_value)
    return None


def _normalize_webhook_transfer_status(value: str) -> str | None:
    normalized = value.strip().lower().replace("-", "_")
    if "." in normalized:
        normalized = normalized.split(".")[-1]
    if normalized in {"pending", "posted", "failed", "returned", "cancelled", "authorized"}:
        return normalized
    if normalized == "canceled":
        return "cancelled"
    return None


def _find_owned_account(store: BankAccountStore, username: str, account_id: str) -> BankAccount:
    for account in store.list_accounts(username):
        if _public_account_id(account) == account_id:
            return account
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")


app = create_app()
