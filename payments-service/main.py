import logging
import os
import secrets
import sqlite3
import math
import time
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from store import PaymentConflict, PaymentsStore

load_dotenv()

DEFAULT_DB = Path(__file__).with_name("payments.db")
DB_PATH = os.getenv("PAYMENTS_DB_PATH", str(DEFAULT_DB))
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s service=payments %(levelname)s %(message)s",
)
logger = logging.getLogger("payments")
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
PAYMENT_TEST_CONTROLS_ENABLED = os.getenv(
    "PAYMENT_TEST_CONTROLS_ENABLED", "false",
).strip().lower() in {"1", "true", "yes", "on"}
TIMEOUT_AFTER_COMMIT_SECONDS = float(
    os.getenv("PAYMENT_TIMEOUT_AFTER_COMMIT_SECONDS", "3.0")
)
ORDERS_PAYMENT_TIMEOUT_SECONDS = float(
    os.getenv("PAYMENT_HTTP_TIMEOUT_SECONDS", "2.0")
)
if (not math.isfinite(TIMEOUT_AFTER_COMMIT_SECONDS)
        or not math.isfinite(ORDERS_PAYMENT_TIMEOUT_SECONDS)
        or TIMEOUT_AFTER_COMMIT_SECONDS < 0
        or ORDERS_PAYMENT_TIMEOUT_SECONDS <= 0):
    raise RuntimeError("Payment timeout settings must be finite nonnegative seconds")


def _is_database_contention(error):
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        base_code = code & 0xFF
        busy_code = getattr(sqlite3, "SQLITE_BUSY", 5)
        locked_code = getattr(sqlite3, "SQLITE_LOCKED", 6)
        return base_code in {busy_code, locked_code}
    message = str(error).lower()
    return "locked" in message or "busy" in message


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)
    amount_cents: int = Field(ge=0, strict=True)
    currency: Literal["MXN"]
    correlation_id: str = Field(min_length=1)
    scenario: Literal["approve", "decline", "timeout_after_commit"] = "approve"


def require_service_token(x_service_token: str | None = Header(default=None)):
    configured_token = os.getenv("SERVICE_TOKEN")
    if not configured_token:
        raise HTTPException(503, "SERVICE_TOKEN is not configured")
    if not x_service_token or not secrets.compare_digest(x_service_token, configured_token):
        raise HTTPException(401, "Missing or invalid X-Service-Token")


def create_app(store=None):
    app = FastAPI(title="Payments")
    app.state.store = store or PaymentsStore(DB_PATH)

    @app.get("/health")
    def health():
        app.state.store.list()
        return {"status": "ok", "service": "payments"}

    @app.post("/payments", dependencies=[Depends(require_service_token)])
    def create_payment(item: PaymentIn):
        if item.scenario != "approve" and (
            APP_ENV != "local" or not PAYMENT_TEST_CONTROLS_ENABLED
        ):
            raise HTTPException(
                403,
                "Payment test scenarios require APP_ENV=local and "
                "PAYMENT_TEST_CONTROLS_ENABLED=true",
            )
        try:
            payment, created = app.state.store.create(
                item.order_id, item.amount_cents, item.currency, item.scenario,
                item.correlation_id,
            )
        except PaymentConflict as error:
            raise HTTPException(409, str(error))
        except sqlite3.OperationalError as error:
            if not _is_database_contention(error):
                raise
            logger.warning(
                "operation=payment outcome=database_busy order_id=%s correlation_id=%s",
                item.order_id, item.correlation_id,
            )
            raise HTTPException(
                503,
                "Payments database is busy; query or retry with the same order_id",
                headers={"Retry-After": "1"},
            ) from error

        logger.info(
            "operation=payment outcome=%s order_id=%s correlation_id=%s "
            "payment_id=%s status=%s scenario=%s",
            payment["status"],
            payment["order_id"], payment["correlation_id"], payment["payment_id"],
            payment["status"], payment["scenario"],
        )
        if item.scenario == "timeout_after_commit" and created:
            delay_seconds = max(
                TIMEOUT_AFTER_COMMIT_SECONDS,
                ORDERS_PAYMENT_TIMEOUT_SECONDS + 0.5,
            )
            logger.info(
                "operation=payment_test_delay outcome=approved_result_committed "
                "order_id=%s correlation_id=%s payment_id=%s delay_seconds=%.2f",
                payment["order_id"], payment["correlation_id"], payment["payment_id"],
                delay_seconds,
            )
            time.sleep(delay_seconds)
        return JSONResponse(status_code=201 if created else 200, content=payment)

    @app.get("/payments")
    def list_payments():
        return app.state.store.list()

    @app.get("/payments/{order_id}")
    def get_payment(order_id: str):
        payment = app.state.store.get(order_id)
        if payment is None:
            raise HTTPException(404, "Payment not found")
        return payment

    return app


app = create_app()
