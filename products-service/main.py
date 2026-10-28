import logging
import os
import secrets
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv

from store import (
    AllocationConflict,
    InsufficientStock,
    ProductNotFound,
    ProductsStore,
)

load_dotenv()

DEFAULT_DB = Path(__file__).with_name("products.db")
DB_PATH = os.getenv("PRODUCTS_DB_PATH", str(DEFAULT_DB))
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s service=products %(levelname)s %(message)s",
)
logger = logging.getLogger("products")


def _is_database_contention(error):
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        base_code = code & 0xFF
        busy_code = getattr(sqlite3, "SQLITE_BUSY", 5)
        locked_code = getattr(sqlite3, "SQLITE_LOCKED", 6)
        return base_code in {busy_code, locked_code}
    message = str(error).lower()
    return "locked" in message or "busy" in message


class AllocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1)
    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, strict=True)
    correlation_id: str = Field(min_length=1)


def require_service_token(x_service_token: str | None = Header(default=None)):
    configured_token = os.getenv("SERVICE_TOKEN")
    if not configured_token:
        raise HTTPException(503, "SERVICE_TOKEN is not configured")
    if not x_service_token or not secrets.compare_digest(x_service_token, configured_token):
        raise HTTPException(401, "Missing or invalid X-Service-Token")


def create_app(store=None):
    app = FastAPI(title="Products")
    app.state.store = store or ProductsStore(DB_PATH)

    @app.get("/health")
    def health():
        app.state.store.list_products()
        return {"status": "ok", "service": "products"}

    @app.get("/products")
    def list_products():
        return app.state.store.list_products()

    @app.get("/products/{product_id}")
    def get_product(product_id: int):
        product = app.state.store.get_product(product_id)
        if product is None:
            raise HTTPException(404, "Product not found")
        return product

    @app.post("/allocations", dependencies=[Depends(require_service_token)])
    def allocate_stock(item: AllocationIn):
        try:
            allocation, created = app.state.store.allocate(
                item.order_id, item.product_id, item.quantity, item.correlation_id,
            )
        except ProductNotFound:
            raise HTTPException(404, "Product not found")
        except InsufficientStock:
            raise HTTPException(409, "Insufficient stock")
        except AllocationConflict as error:
            raise HTTPException(409, str(error))
        except sqlite3.OperationalError as error:
            if not _is_database_contention(error):
                raise
            logger.warning(
                "operation=allocate outcome=database_busy order_id=%s correlation_id=%s",
                item.order_id, item.correlation_id,
            )
            raise HTTPException(
                503,
                "Products database is busy; retry the allocation with the same order_id",
                headers={"Retry-After": "1"},
            ) from error
        logger.info(
            "operation=allocate outcome=%s order_id=%s correlation_id=%s allocation_id=%s",
            allocation["status"], allocation["order_id"], allocation["correlation_id"],
            allocation["allocation_id"],
        )
        return JSONResponse(status_code=201 if created else 200, content=allocation)

    @app.get("/allocations/{order_id}")
    def get_allocation(order_id: str):
        allocation = app.state.store.get_allocation(order_id)
        if allocation is None:
            raise HTTPException(404, "Allocation not found")
        return allocation

    @app.post(
        "/allocations/{order_id}/release",
        dependencies=[Depends(require_service_token)],
    )
    def release_allocation(
        order_id: str,
        x_correlation_id: str | None = Header(default=None),
    ):
        try:
            allocation = app.state.store.release(order_id)
        except sqlite3.OperationalError as error:
            if not _is_database_contention(error):
                raise
            logger.warning(
                "operation=release outcome=database_busy order_id=%s "
                "correlation_id=%s",
                order_id, x_correlation_id or "unknown",
            )
            raise HTTPException(
                503,
                "Products database is busy; retry the release with the same order_id",
                headers={"Retry-After": "1"},
            ) from error
        if allocation is None:
            raise HTTPException(404, "Allocation not found")
        logger.info(
            "operation=release outcome=%s order_id=%s correlation_id=%s allocation_id=%s",
            allocation["status"], allocation["order_id"],
            x_correlation_id or allocation["correlation_id"],
            allocation["allocation_id"],
        )
        return allocation

    return app


app = create_app()
