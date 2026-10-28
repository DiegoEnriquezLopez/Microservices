import argparse
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pika
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from broker import QUEUE, connect, declare_topology

load_dotenv()

DEFAULT_DB = Path(__file__).with_name("notifications.db")
DB_PATH = os.getenv("NOTIFICATIONS_DB_PATH", str(DEFAULT_DB))
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s service=notifications %(levelname)s %(message)s",
)
logger = logging.getLogger("notifications")


class OrderConfirmed(BaseModel):
    event_id: str = Field(min_length=1)
    event_type: Literal["OrderConfirmed"]
    schema_version: Literal[1]
    occurred_at: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, strict=True)
    total_cents: int = Field(ge=0, strict=True)
    currency: Literal["MXN"]
    correlation_id: str = Field(min_length=1)


class NotificationConflict(Exception):
    pass


class NotificationsStore:
    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS confirmations (
                    order_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    product_id INTEGER NOT NULL CHECK (product_id > 0),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
                    currency TEXT NOT NULL CHECK (currency = 'MXN'),
                    correlation_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'confirmed'),
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_event_ids (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );
                """
            )

    def process_event(self, raw_event):
        event = OrderConfirmed.model_validate(raw_event)
        confirmation = {
            "order_id": event.order_id,
            "event_id": event.event_id,
            "product_id": event.product_id,
            "quantity": event.quantity,
            "total_cents": event.total_cents,
            "currency": event.currency,
            "correlation_id": event.correlation_id,
            "status": "confirmed",
        }
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            processed = connection.execute(
                "SELECT order_id FROM processed_event_ids WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if processed:
                return "duplicate", self._get_confirmation(connection, processed["order_id"])

            existing = self._get_confirmation(connection, event.order_id)
            if existing:
                actual = (
                    existing["product_id"], existing["quantity"], existing["total_cents"],
                    existing["currency"], existing["correlation_id"],
                )
                expected = (
                    event.product_id, event.quantity, event.total_cents,
                    event.currency, event.correlation_id,
                )
                if actual != expected:
                    raise NotificationConflict("Conflicting confirmation event for order")
                connection.execute(
                    "INSERT INTO processed_event_ids (event_id, order_id, processed_at) "
                    "VALUES (?, ?, ?)",
                    (event.event_id, event.order_id, now),
                )
                return "duplicate", existing

            connection.execute(
                "INSERT INTO confirmations "
                "(order_id, event_id, product_id, quantity, total_cents, currency, "
                "correlation_id, status, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.order_id, event.event_id, event.product_id, event.quantity,
                 event.total_cents, event.currency, event.correlation_id, "confirmed", now),
            )
            connection.execute(
                "INSERT INTO processed_event_ids (event_id, order_id, processed_at) "
                "VALUES (?, ?, ?)",
                (event.event_id, event.order_id, now),
            )
            confirmation["processed_at"] = now
            return "created", confirmation

    @staticmethod
    def _get_confirmation(connection, order_id):
        row = connection.execute(
            "SELECT order_id, event_id, product_id, quantity, total_cents, currency, "
            "correlation_id, status, processed_at FROM confirmations WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_confirmations(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT order_id, event_id, product_id, quantity, total_cents, currency, "
                "correlation_id, status, processed_at FROM confirmations ORDER BY processed_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_processed_event_ids(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT event_id, order_id, processed_at FROM processed_event_ids "
                "ORDER BY processed_at"
            ).fetchall()
        return [dict(row) for row in rows]


def handle_event(event, store=None):
    store = store or NotificationsStore()
    return store.process_event(event)


def on_message(channel, method, properties, body, store=None):
    store = store or NotificationsStore()
    event = {}
    try:
        event = json.loads(body)
        if not isinstance(event, dict):
            event = {}
        outcome, confirmation = store.process_event(event)
    except (ValueError, ValidationError, UnicodeDecodeError, NotificationConflict,
            sqlite3.IntegrityError) as error:
        logger.error(
            "operation=consume outcome=rejected order_id=%s correlation_id=%s "
            "event_id=%s error=%s",
            event.get("order_id", "unknown"),
            event.get("correlation_id", "unknown"),
            event.get("event_id", "unknown"), str(error),
        )
        channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
        return
    except Exception as error:
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        logger.exception(
            "operation=consume outcome=requeued order_id=%s correlation_id=%s "
            "event_id=%s error=%s",
            event.get("order_id", "unknown"),
            event.get("correlation_id", "unknown"),
            event.get("event_id", "unknown"), type(error).__name__,
        )
        return

    # The database transaction commits in process_event before this acknowledgement.
    channel.basic_ack(delivery_tag=method.delivery_tag)
    logger.info(
        "operation=consume outcome=%s order_id=%s correlation_id=%s event_id=%s "
        "confirmation_status=%s",
        outcome, confirmation["order_id"], confirmation["correlation_id"],
        event["event_id"], confirmation["status"],
    )


def consume():
    connection = connect()
    try:
        channel = connection.channel()
        declare_topology(channel)
        channel.basic_qos(prefetch_count=1)
        store = NotificationsStore()
        channel.basic_consume(
            queue=QUEUE,
            on_message_callback=lambda ch, method, props, body: on_message(
                ch, method, props, body, store,
            ),
            auto_ack=False,
        )
        logger.info("operation=start outcome=waiting queue=%s", QUEUE)
        channel.start_consuming()
    finally:
        if connection.is_open:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description="Notifications worker and confirmation inspection")
    parser.add_argument(
        "--list-confirmations", action="store_true",
        help="Print persisted purchase confirmations and exit",
    )
    parser.add_argument(
        "--list-processed-event-ids", action="store_true",
        help="Print persisted processed event IDs and exit",
    )
    args = parser.parse_args()
    if args.list_confirmations:
        print(json.dumps(NotificationsStore().list_confirmations(), indent=2))
        return
    if args.list_processed_event_ids:
        print(json.dumps(NotificationsStore().list_processed_event_ids(), indent=2))
        return
    try:
        consume()
    except (pika.exceptions.AMQPError, OSError) as error:
        raise SystemExit(f"RabbitMQ unavailable. Start the broker and retry. {error}")


if __name__ == "__main__":
    main()
