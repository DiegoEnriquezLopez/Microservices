import json
import logging
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv

from broker import publish_event
from store import OrdersStore, RequestKeyConflict
from workflow import CheckoutCoordinator

load_dotenv()

DEFAULT_DB = Path(__file__).with_name("orders.db")
DB_PATH = os.getenv("ORDERS_DB_PATH", str(DEFAULT_DB))
MAX_ORDER_QUANTITY = int(os.getenv("MAX_ORDER_QUANTITY", "10"))
if MAX_ORDER_QUANTITY < 1:
    raise ValueError("MAX_ORDER_QUANTITY must be a positive integer")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s service=orders %(levelname)s %(message)s",
)
logger = logging.getLogger("orders")


class CheckoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str = Field(min_length=1, max_length=128)
    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, le=MAX_ORDER_QUANTITY, strict=True)


def _response_order(store, order):
    result = dict(order)
    event = store.get_event_for_order(order["order_id"])
    result["event_publication_status"] = event["publication_status"] if event else None
    result["event_id"] = event["event_id"] if event else None
    return result


def create_app(store=None, coordinator=None):
    app = FastAPI(title="Orders")
    app.state.store = store or OrdersStore(DB_PATH)
    app.state.coordinator = coordinator or CheckoutCoordinator(
        app.state.store, publisher=publish_event,
    )

    @app.get("/health")
    def health():
        app.state.store.list()
        return {"status": "ok", "service": "orders"}

    @app.post("/orders")
    def create_order(item: CheckoutIn):
        try:
            order, created = app.state.store.create_or_get(
                item.request_key, item.product_id, item.quantity,
            )
        except RequestKeyConflict as error:
            saved = app.state.store.get_by_request_key(item.request_key)
            response = {
                "detail": str(error),
                "order_id": saved["order_id"] if saved else None,
                "status": saved["status"] if saved else None,
            }
            return JSONResponse(status_code=409, content=response)
        except sqlite3.OperationalError:
            raise HTTPException(503, "Orders database is busy; retry the same request key")

        order, status_code = app.state.coordinator.resume(order["order_id"])
        if order is None:
            raise HTTPException(404, "Order not found")
        if created and status_code == 200:
            status_code = 201
        elif not created and status_code not in {202, 402, 404}:
            status_code = 200
        return JSONResponse(
            status_code=status_code,
            content=_response_order(app.state.store, order),
        )

    @app.get("/orders/{order_id}")
    def get_order(order_id: str):
        order = app.state.store.get(order_id)
        if order is None:
            raise HTTPException(404, "Order not found")
        return _response_order(app.state.store, order)

    @app.get("/orders")
    def list_orders():
        return [
            _response_order(app.state.store, order)
            for order in app.state.store.list()
        ]

    @app.post("/orders/{order_id}/resume")
    def resume_order(order_id: str):
        order, status_code = app.state.coordinator.resume(order_id)
        if order is None:
            raise HTTPException(404, "Order not found")
        return JSONResponse(
            status_code=status_code,
            content=_response_order(app.state.store, order),
        )

    @app.post("/events/publish-pending")
    def publish_pending_events():
        published = app.state.coordinator.publish_pending()
        pending = app.state.store.pending_events()
        return {
            "published_count": published,
            "pending_count": len(pending),
            "pending_event_ids": [row["event_id"] for row in pending],
        }

    @app.post("/events/{event_id}/replay")
    def replay_event(event_id: str):
        record = app.state.store.get_event(event_id)
        if record is None:
            raise HTTPException(404, "Event not found")
        event = {}
        try:
            event = json.loads(record["payload"])
            publish_event(event)
        except Exception as error:
            logger.warning(
                "operation=replay outcome=pending event_id=%s order_id=%s "
                "correlation_id=%s error=%s",
                event_id, record["order_id"],
                event.get("correlation_id", "unknown"), type(error).__name__,
            )
            raise HTTPException(503, "Event publication outcome is not confirmed")
        app.state.store.mark_published(event_id)
        logger.info(
            "operation=replay outcome=published event_id=%s order_id=%s "
            "correlation_id=%s",
            event_id, record["order_id"], event.get("correlation_id", "unknown"),
        )
        return app.state.store.get_event(event_id)

    return app


app = create_app()
