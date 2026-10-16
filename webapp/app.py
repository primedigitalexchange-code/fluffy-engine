"""FastAPI demo dashboard for in-memory account payments."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

try:  # Pydantic v2
    from pydantic import field_validator
except ImportError:  # pragma: no cover - compatibility for older Pydantic
    from pydantic import validator as field_validator

from bank_account import BankAccountStore, NotFoundError, Payment, ValidationError
from bank_profile import build_bank_profile
from banking_connector import AppCredentials, load_app_credentials_from_env

security = HTTPBasic()


class PaymentRequest(BaseModel):
    source_account_number: str = Field(min_length=1)
    destination_user_id: str = Field(min_length=1)
    destination_account_number: str = Field(min_length=1)
    amount: Decimal
    memo: str = Field(min_length=1, max_length=140)

    @field_validator(
        "source_account_number",
        "destination_user_id",
        "destination_account_number",
        "memo",
    )
    def _normalize_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must be a non-empty string")
        return normalized

    @field_validator("amount")
    def _normalize_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be a finite decimal amount")
        try:
            quantized = value.quantize(Decimal("0.01"))
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise ValueError("amount must be a valid decimal amount") from exc
        if quantized != value:
            raise ValueError("amount must have no more than 2 decimal places")
        if quantized <= Decimal("0.00"):
            raise ValueError("amount must be greater than 0")
        return quantized


def _get_store(request: Request) -> BankAccountStore:
    return request.app.state.store


def _get_credentials(request: Request) -> AppCredentials:
    return request.app.state.credentials


def _get_current_user_id(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    configured_credentials = _get_credentials(request)
    username_matches = secrets.compare_digest(
        credentials.username,
        configured_credentials.username,
    )
    password_matches = secrets.compare_digest(
        credentials.password,
        configured_credentials.password,
    )
    if not username_matches or not password_matches:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return configured_credentials.username


def _mask_account_number(account_number: str) -> str:
    normalized = account_number.strip()
    if len(normalized) <= 4:
        return normalized
    return f"****{normalized[-4:]}"


def _serialize_payment_receipt(payment: Payment) -> dict[str, str]:
    return {
        "payment_id": payment.payment_id,
        "source_user_id": payment.source_user_id,
        "source_account_number": _mask_account_number(payment.source_account_number),
        "destination_user_id": payment.destination_user_id,
        "destination_account_number": _mask_account_number(payment.destination_account_number),
        "amount": f"{payment.amount:.2f}",
        "memo": payment.memo,
        "created_at": payment.created_at.isoformat(),
    }


def _submit_payment(
    store: BankAccountStore,
    current_user_id: str,
    payment_request: PaymentRequest,
) -> dict[str, str]:
    owned_account_numbers = {
        account.account_number
        for account in store.list_accounts(current_user_id)
    }
    if payment_request.source_account_number not in owned_account_numbers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="source account is not owned by the authenticated user",
        )

    try:
        payment = store.make_payment(
            source_user_id=current_user_id,
            source_account_number=payment_request.source_account_number,
            destination_user_id=payment_request.destination_user_id,
            destination_account_number=payment_request.destination_account_number,
            amount=payment_request.amount,
            memo=payment_request.memo,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="destination account not found",
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return _serialize_payment_receipt(payment)


def _render_dashboard(
    store: BankAccountStore,
    current_user_id: str,
    *,
    status_message: str | None = None,
    status_class: str = "success",
    form_values: dict[str, str] | None = None,
) -> str:
    form_values = form_values or {}
    accounts = store.list_accounts(current_user_id)
    options = "\n".join(
        (
            f'<option value="{escape(account.account_number)}"'
            f'{" selected" if form_values.get("source_account_number") == account.account_number else ""}>'
            f'{escape(account.account_type.title())} ({escape(_mask_account_number(account.account_number))})'
            "</option>"
        )
        for account in accounts
    )
    if not options:
        options = '<option value="">No source accounts available</option>'

    status_block = ""
    if status_message is not None:
        status_block = (
            f'<p data-testid="payment-status" class="status {escape(status_class)}">'
            f"{escape(status_message)}</p>"
        )

    account_sections = []
    for account in accounts:
        transactions = store.list_transactions(current_user_id, account.account_number)
        transaction_items = []
        for transaction in transactions:
            details = [
                escape(transaction.transaction_type),
                f"${transaction.amount:.2f}",
            ]
            if transaction.memo:
                details.append(escape(transaction.memo))
            if transaction.counterparty_account_number:
                details.append(escape(_mask_account_number(transaction.counterparty_account_number)))
            transaction_items.append(f"<li>{' — '.join(details)}</li>")
        transactions_markup = (
            "<ul>" + "".join(transaction_items) + "</ul>"
            if transaction_items
            else "<p>No transactions yet.</p>"
        )
        account_sections.append(
            "<section>"
            f"<h2>{escape(account.account_type.title())} {escape(_mask_account_number(account.account_number))}</h2>"
            f"<p>Balance: ${account.balance:.2f}</p>"
            f"<p>Status: {escape(account.status.value)}</p>"
            f"{transactions_markup}"
            "</section>"
        )

    return (
        "<!doctype html>"
        "<html><head><title>fluffy-engine payments demo</title></head><body>"
        f"<h1>Payments dashboard for {escape(current_user_id)}</h1>"
        "<p>This dashboard is an in-memory demo only. It does not move real money.</p>"
        f"{status_block}"
        '<form method="post" action="/dashboard/payments">'
        '<label>Source account'
        f'<select name="source_account_number">{options}</select>'
        "</label>"
        '<label>Destination user'
        f'<input name="destination_user_id" value="{escape(form_values.get("destination_user_id", ""))}" />'
        "</label>"
        '<label>Destination account'
        f'<input name="destination_account_number" value="{escape(form_values.get("destination_account_number", ""))}" />'
        "</label>"
        '<label>Amount'
        f'<input name="amount" value="{escape(form_values.get("amount", ""))}" />'
        "</label>"
        '<label>Memo'
        f'<input name="memo" value="{escape(form_values.get("memo", ""))}" />'
        "</label>"
        '<button type="submit">Send payment</button>'
        "</form>"
        "<hr />"
        + "".join(account_sections)
        + "</body></html>"
    )


def _format_pydantic_error(error: PydanticValidationError) -> str:
    first_error = error.errors()[0]
    return str(first_error.get("msg", "invalid payment request"))


def create_app(*, store: BankAccountStore, credentials: AppCredentials) -> FastAPI:
    app = FastAPI(title="fluffy-engine payment demo")
    app.state.store = store
    app.state.credentials = credentials

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

    @app.get("/dashboard", response_class=HTMLResponse)
    def get_dashboard(
        current_user_id: Annotated[str, Depends(_get_current_user_id)],
        store: Annotated[BankAccountStore, Depends(_get_store)],
    ) -> HTMLResponse:
        return HTMLResponse(_render_dashboard(store, current_user_id))

    @app.post("/dashboard/payments", response_class=HTMLResponse)
    def submit_dashboard_payment(
        source_account_number: Annotated[str, Form()],
        destination_user_id: Annotated[str, Form()],
        destination_account_number: Annotated[str, Form()],
        amount: Annotated[str, Form()],
        memo: Annotated[str, Form()],
        current_user_id: Annotated[str, Depends(_get_current_user_id)],
        store: Annotated[BankAccountStore, Depends(_get_store)],
    ) -> HTMLResponse:
        form_values = {
            "source_account_number": source_account_number,
            "destination_user_id": destination_user_id,
            "destination_account_number": destination_account_number,
            "amount": amount,
            "memo": memo,
        }
        try:
            payment_request = PaymentRequest(
                source_account_number=source_account_number,
                destination_user_id=destination_user_id,
                destination_account_number=destination_account_number,
                amount=amount,
                memo=memo,
            )
        except PydanticValidationError as exc:
            return HTMLResponse(
                _render_dashboard(
                    store,
                    current_user_id,
                    status_message=_format_pydantic_error(exc),
                    status_class="error",
                    form_values=form_values,
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            receipt = _submit_payment(store, current_user_id, payment_request)
        except HTTPException as exc:
            return HTMLResponse(
                _render_dashboard(
                    store,
                    current_user_id,
                    status_message=str(exc.detail),
                    status_class="error",
                    form_values=form_values,
                ),
                status_code=exc.status_code,
            )

        return HTMLResponse(
            _render_dashboard(
                store,
                current_user_id,
                status_message=(
                    f"Payment {receipt['payment_id']} submitted successfully for ${receipt['amount']}."
                ),
            ),
        )

    @app.post("/api/payments")
    def create_payment(
        payment_request: PaymentRequest,
        current_user_id: Annotated[str, Depends(_get_current_user_id)],
        store: Annotated[BankAccountStore, Depends(_get_store)],
    ) -> dict[str, dict[str, str]]:
        return {"payment": _submit_payment(store, current_user_id, payment_request)}

    return app


def create_demo_app() -> FastAPI:
    credentials = load_app_credentials_from_env()
    store = BankAccountStore()
    profile = build_bank_profile()
    store.create_account(
        user_id=credentials.username,
        account_number=profile.account_number,
        account_type="checking",
        balance=profile.balance_usd,
        routing_number=profile.routing_number,
        bank_name=profile.bank_name,
        bank_address=profile.bank_address,
        city=profile.city,
        state=profile.state,
        postal_code=profile.postal_code,
    )
    store.create_account(
        user_id=credentials.username,
        account_number="000123456780",
        account_type="savings",
        balance="1250.00",
        routing_number=profile.routing_number,
        bank_name=profile.bank_name,
        bank_address=profile.bank_address,
        city=profile.city,
        state=profile.state,
        postal_code=profile.postal_code,
    )
    store.create_account(
        user_id="demo-vendor",
        account_number="000123456781",
        account_type="checking",
        balance="500.00",
        routing_number=profile.routing_number,
        bank_name=profile.bank_name,
        bank_address=profile.bank_address,
        city=profile.city,
        state=profile.state,
        postal_code=profile.postal_code,
    )
    return create_app(store=store, credentials=credentials)
