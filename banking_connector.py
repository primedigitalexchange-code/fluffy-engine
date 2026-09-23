"""Plaid banking connector with environment-driven live banking support."""

from __future__ import annotations

import base64
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import logging
import os
import re
from time import monotonic, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - exercised only when dependency is missing.
    Fernet = None

    class InvalidToken(Exception):
        """Fallback invalid token error when cryptography is unavailable."""


_PLAID_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}
_TOKEN_RE = re.compile(r"\b(?:access|public|link)-[A-Za-z0-9_-]+\b")


class BankingConnectorError(RuntimeError):
    """Base exception for banking connector failures."""


class ConfigurationError(BankingConnectorError):
    """Raised when live banking configuration is missing or invalid."""


class BankingAuthenticationError(BankingConnectorError):
    """Raised when the Plaid API rejects authentication."""


class BankingAPIError(BankingConnectorError):
    """Raised when a Plaid API call fails."""


class TokenStorageError(BankingConnectorError):
    """Raised when an encrypted token cannot be loaded or stored."""


class BankingRequestValidationError(BankingConnectorError):
    """Raised when an API-style banking request payload is invalid."""


@dataclass(frozen=True)
class BankingConfig:
    mode: str
    provider: str
    plaid_client_id: str | None
    plaid_secret: str | None
    plaid_environment: str
    token_encryption_key: str | None
    rate_limit_per_minute: int = 30
    timeout_seconds: int = 10
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BankingConfig":
        source = os.environ if env is None else env
        mode = source.get("FLUFFY_ENGINE_BANK_DATA_MODE", "mock").strip().lower()
        provider = source.get("FLUFFY_ENGINE_BANK_PROVIDER", "plaid").strip().lower()
        plaid_environment = source.get("PLAID_ENV", "sandbox").strip().lower()
        token_encryption_key = source.get("BANKING_TOKEN_ENCRYPTION_KEY")

        if mode not in {"mock", "live"}:
            raise ConfigurationError("FLUFFY_ENGINE_BANK_DATA_MODE must be mock or live")
        if provider != "plaid":
            raise ConfigurationError("Only the Plaid provider is currently supported")
        if plaid_environment not in _PLAID_URLS:
            raise ConfigurationError("PLAID_ENV must be sandbox, development, or production")

        rate_limit_per_minute = int(source.get("BANKING_RATE_LIMIT_PER_MINUTE", "30"))
        timeout_seconds = int(source.get("BANKING_TIMEOUT_SECONDS", "10"))
        log_level = source.get("BANKING_LOG_LEVEL", "INFO").strip().upper()
        plaid_client_id = source.get("PLAID_CLIENT_ID")
        plaid_secret = source.get("PLAID_SECRET")

        if mode == "live":
            missing = [
                name
                for name, value in (
                    ("PLAID_CLIENT_ID", plaid_client_id),
                    ("PLAID_SECRET", plaid_secret),
                    ("BANKING_TOKEN_ENCRYPTION_KEY", token_encryption_key),
                )
                if not value
            ]
            if missing:
                raise ConfigurationError(
                    f"Missing required live banking environment variables: {', '.join(missing)}"
                )

        return cls(
            mode=mode,
            provider=provider,
            plaid_client_id=plaid_client_id,
            plaid_secret=plaid_secret,
            plaid_environment=plaid_environment,
            token_encryption_key=token_encryption_key,
            rate_limit_per_minute=rate_limit_per_minute,
            timeout_seconds=timeout_seconds,
            log_level=log_level,
        )


@dataclass(frozen=True)
class LiveBankAccountData:
    account_id: str
    item_id: str
    account_number: str
    routing_number: str
    balance: Decimal
    account_type: str
    bank_name: str | None = None
    bank_address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    provider: str = "plaid"


@dataclass(frozen=True)
class BankTransaction:
    transaction_id: str
    account_id: str
    amount: Decimal
    name: str
    posted_on: date
    pending: bool
    iso_currency_code: str | None = None
    merchant_name: str | None = None


@dataclass(frozen=True)
class ConnectedBankSession:
    item_id: str
    access_token_reference: str
    accounts: tuple[LiveBankAccountData, ...]


class EncryptedTokenStore:
    """In-memory encrypted storage for Plaid access tokens."""

    def __init__(self, encryption_key: str) -> None:
        if Fernet is None:
            raise ConfigurationError(
                "cryptography is required for encrypted token storage; install it before using live banking"
            )
        self._cipher = Fernet(_normalize_fernet_key(encryption_key))
        self._tokens: dict[str, str] = {}

    def save_token(self, token_reference: str, access_token: str) -> str:
        normalized_reference = _require_string("token_reference", token_reference)
        normalized_token = _require_string("access_token", access_token)
        ciphertext = self._cipher.encrypt(normalized_token.encode("utf-8")).decode("utf-8")
        self._tokens[normalized_reference] = ciphertext
        return normalized_reference

    def load_token(self, token_reference: str) -> str:
        normalized_reference = _require_string("token_reference", token_reference)
        try:
            ciphertext = self._tokens[normalized_reference]
        except KeyError as exc:
            raise TokenStorageError("No token is stored for the supplied reference") from exc
        try:
            return self._cipher.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise TokenStorageError("Stored token could not be decrypted") from exc


class PlaidConnector:
    """Thin Plaid REST connector for link, auth, balances, and transactions."""

    def __init__(
        self,
        config: BankingConfig,
        *,
        token_store: EncryptedTokenStore | None = None,
        opener: Callable[..., Any] = urlopen,
        monotonic_clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        if config.mode != "live":
            raise ConfigurationError("PlaidConnector requires FLUFFY_ENGINE_BANK_DATA_MODE=live")
        self._config = config
        self._token_store = token_store or EncryptedTokenStore(config.token_encryption_key or "")
        self._opener = opener
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._logger = logger or logging.getLogger(__name__)
        self._logger.setLevel(getattr(logging, config.log_level, logging.INFO))
        self._recent_calls: deque[float] = deque()

    @property
    def base_url(self) -> str:
        return _PLAID_URLS[self._config.plaid_environment]

    def create_link_token(
        self,
        *,
        user_id: str,
        client_name: str = "fluffy-engine",
        products: Iterable[str] = ("auth", "transactions"),
        country_codes: Iterable[str] = ("US",),
        language: str = "en",
    ) -> dict[str, str]:
        response = self._request(
            "/link/token/create",
            {
                "client_name": _require_string("client_name", client_name),
                "language": _require_string("language", language),
                "country_codes": list(country_codes),
                "products": list(products),
                "user": {"client_user_id": _require_string("user_id", user_id)},
            },
        )
        return {
            "link_token": response["link_token"],
            "expiration": response["expiration"],
            "request_id": response["request_id"],
        }

    def exchange_public_token(self, public_token: str) -> dict[str, str]:
        response = self._request(
            "/item/public_token/exchange",
            {"public_token": _require_string("public_token", public_token)},
        )
        item_id = response["item_id"]
        self._token_store.save_token(item_id, response["access_token"])
        self._logger.info("Stored Plaid access token for item %s", item_id)
        return {
            "item_id": item_id,
            "access_token_reference": item_id,
            "request_id": response["request_id"],
        }

    def refresh_access_token(self, item_id: str) -> dict[str, str]:
        access_token = self._token_store.load_token(item_id)
        response = self._request(
            "/item/access_token/invalidate",
            {"access_token": access_token},
        )
        self._token_store.save_token(item_id, response["new_access_token"])
        self._logger.info("Rotated Plaid access token for item %s", item_id)
        return {
            "item_id": item_id,
            "access_token_reference": item_id,
            "request_id": response["request_id"],
        }

    def connect_account(self, *, user_id: str, public_token: str) -> ConnectedBankSession:
        exchange = self.exchange_public_token(public_token)
        accounts = self.get_account_information(exchange["item_id"])
        self._logger.info("Connected %s Plaid account(s) for user %s", len(accounts), user_id)
        return ConnectedBankSession(
            item_id=exchange["item_id"],
            access_token_reference=exchange["access_token_reference"],
            accounts=tuple(accounts),
        )

    def get_account_information(
        self,
        item_id: str,
        *,
        account_ids: Iterable[str] | None = None,
    ) -> list[LiveBankAccountData]:
        access_token = self._token_store.load_token(item_id)
        auth_response = self._request("/auth/get", {"access_token": access_token})
        balance_response = self._request("/accounts/balance/get", {"access_token": access_token})
        item_response = self._request("/item/get", {"access_token": access_token})

        institution_name = None
        institution_id = item_response.get("item", {}).get("institution_id")
        if institution_id:
            institution_response = self._request(
                "/institutions/get_by_id",
                {
                    "institution_id": institution_id,
                    "country_codes": ["US"],
                },
            )
            institution_name = institution_response.get("institution", {}).get("name")

        balance_lookup = {
            account["account_id"]: account
            for account in balance_response.get("accounts", [])
        }
        wanted_account_ids = set(account_ids or [])
        live_accounts: list[LiveBankAccountData] = []
        for numbers in auth_response.get("numbers", {}).get("ach", []):
            account_id = numbers["account_id"]
            if wanted_account_ids and account_id not in wanted_account_ids:
                continue
            account_payload = balance_lookup.get(account_id, {})
            balances = account_payload.get("balances", {})
            current_balance = balances.get("available")
            if current_balance is None:
                current_balance = balances.get("current", "0.00")
            live_accounts.append(
                LiveBankAccountData(
                    account_id=account_id,
                    item_id=item_id,
                    account_number=numbers["account"],
                    routing_number=numbers["routing"],
                    balance=Decimal(str(current_balance)).quantize(Decimal("0.01")),
                    account_type=account_payload.get("subtype") or account_payload.get("type") or "checking",
                    bank_name=institution_name,
                )
            )
        return live_accounts

    def get_transaction_history(
        self,
        item_id: str,
        *,
        start_date: date | str,
        end_date: date | str,
        account_id: str | None = None,
    ) -> list[BankTransaction]:
        normalized_start = _normalize_date(start_date)
        normalized_end = _normalize_date(end_date)
        if normalized_start > normalized_end:
            raise ValueError("start_date must be on or before end_date")

        payload: dict[str, Any] = {
            "access_token": self._token_store.load_token(item_id),
            "start_date": normalized_start.isoformat(),
            "end_date": normalized_end.isoformat(),
        }
        if account_id is not None:
            payload["options"] = {"account_ids": [_require_string("account_id", account_id)]}

        response = self._request("/transactions/get", payload)
        return [
            BankTransaction(
                transaction_id=transaction["transaction_id"],
                account_id=(transaction.get("account_id") or ""),
                amount=Decimal(str(transaction["amount"])).quantize(Decimal("0.01")),
                name=transaction["name"],
                posted_on=_normalize_date(transaction["date"]),
                pending=bool(transaction.get("pending", False)),
                iso_currency_code=transaction.get("iso_currency_code"),
                merchant_name=transaction.get("merchant_name"),
            )
            for transaction in response.get("transactions", [])
        ]

    def _request(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._enforce_rate_limit()
        request_body = {
            "client_id": self._config.plaid_client_id,
            "secret": self._config.plaid_secret,
            **payload,
        }
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(request_body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        self._logger.info("Calling Plaid endpoint %s", path)
        try:
            response = self._opener(request, timeout=self._config.timeout_seconds)
            return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = _sanitize_message(_extract_http_error_message(exc))
            if exc.code in {400, 401, 403}:
                raise BankingAuthenticationError(message) from exc
            raise BankingAPIError(message) from exc
        except URLError as exc:
            raise BankingAPIError("Unable to reach the Plaid API") from exc

    def _enforce_rate_limit(self) -> None:
        limit = self._config.rate_limit_per_minute
        if limit <= 0:
            return
        now = self._monotonic_clock()
        while self._recent_calls and now - self._recent_calls[0] >= 60:
            self._recent_calls.popleft()
        if len(self._recent_calls) >= limit:
            wait_seconds = 60 - (now - self._recent_calls[0])
            if wait_seconds > 0:
                self._logger.info("Sleeping %.2f seconds to respect Plaid rate limits", wait_seconds)
                self._sleeper(wait_seconds)
            now = self._monotonic_clock()
            while self._recent_calls and now - self._recent_calls[0] >= 60:
                self._recent_calls.popleft()
        self._recent_calls.append(self._monotonic_clock())


def build_banking_connector_from_env(env: Mapping[str, str] | None = None) -> PlaidConnector:
    return PlaidConnector(BankingConfig.from_env(env))


def handle_banking_request(
    connector: PlaidConnector,
    *,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_method = _require_string("method", method).upper()
    normalized_path = _require_string("path", path)
    payload = body or {}
    if not isinstance(payload, Mapping):
        raise BankingRequestValidationError("body must be a JSON object")

    try:
        if normalized_method != "POST":
            return {
                "status": 405,
                "body": {"error": "method_not_allowed", "message": "Only POST is supported"},
            }
        if normalized_path == "/bank/link-token":
            result = connector.create_link_token(
                user_id=_require_string("user_id", payload.get("user_id")),
            )
            return {"status": 200, "body": result}
        if normalized_path == "/bank/connect":
            session = connector.connect_account(
                user_id=_require_string("user_id", payload.get("user_id")),
                public_token=_require_string("public_token", payload.get("public_token")),
            )
            return {
                "status": 200,
                "body": {
                    "item_id": session.item_id,
                    "access_token_reference": session.access_token_reference,
                    "accounts": [
                        {
                            "account_id": account.account_id,
                            "account_type": account.account_type,
                            "balance": f"{account.balance:.2f}",
                            "bank_name": account.bank_name,
                            "masked_account_number": _mask_account_number(account.account_number),
                        }
                        for account in session.accounts
                    ],
                },
            }
        return {
            "status": 404,
            "body": {"error": "not_found", "message": f"Unsupported path: {normalized_path}"},
        }
    except BankingAuthenticationError as exc:
        return {"status": 401, "body": {"error": "authentication_failed", "message": str(exc)}}
    except (BankingRequestValidationError, ValueError, TokenStorageError) as exc:
        return {"status": 400, "body": {"error": "invalid_request", "message": str(exc)}}
    except BankingAPIError as exc:
        return {"status": 502, "body": {"error": "banking_provider_error", "message": str(exc)}}


def _mask_account_number(account_number: str) -> str:
    normalized = _require_string("account_number", account_number)
    if len(normalized) <= 4:
        return normalized
    return f"{'*' * (len(normalized) - 4)}{normalized[-4:]}"


def _normalize_fernet_key(key: str) -> bytes:
    normalized_key = _require_string("BANKING_TOKEN_ENCRYPTION_KEY", key)
    if len(normalized_key) == 44:
        try:
            base64.urlsafe_b64decode(normalized_key.encode("utf-8"))
            return normalized_key.encode("utf-8")
        except Exception:
            pass
    return base64.urlsafe_b64encode(hashlib.sha256(normalized_key.encode("utf-8")).digest())


def _normalize_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(_require_string("date", value))


def _require_string(field_name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized


def _extract_http_error_message(exc: HTTPError) -> str:
    raw_body = exc.read().decode("utf-8", errors="replace")
    if not raw_body:
        return exc.reason or "Plaid API request failed"
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body
    return payload.get("error_message") or payload.get("display_message") or exc.reason or "Plaid API request failed"


def _sanitize_message(message: str) -> str:
    redacted = _TOKEN_RE.sub("[REDACTED_TOKEN]", message)
    return redacted or "Plaid API request failed"
