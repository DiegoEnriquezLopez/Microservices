import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


class RequestKeyConflict(Exception):
    pass


class OrdersStore:
    ORDER_COLUMNS = (
        "order_id, request_key, product_id, quantity, unit_price_cents, total_cents, "
        "currency, status, rejection_reason, last_error, allocation_id, allocation_status, "
        "payment_id, payment_status, correlation_id, created_at, updated_at"
    )

    def __init__(self, db_path="orders.db"):
        self.db_path = str(db_path)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    product_id INTEGER NOT NULL CHECK (product_id > 0),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    unit_price_cents INTEGER CHECK (unit_price_cents >= 0),
                    total_cents INTEGER CHECK (total_cents >= 0),
                    currency TEXT NOT NULL DEFAULT 'MXN' CHECK (currency = 'MXN'),
                    status TEXT NOT NULL CHECK (status IN (
                        'processing', 'recovery_required', 'compensation_pending',
                        'rejected', 'confirmed'
                    )),
                    rejection_reason TEXT,
                    last_error TEXT,
                    allocation_id TEXT,
                    allocation_status TEXT,
                    payment_id TEXT,
                    payment_status TEXT,
                    correlation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
                    event_type TEXT NOT NULL CHECK (event_type = 'OrderConfirmed'),
                    payload TEXT NOT NULL,
                    publication_status TEXT NOT NULL CHECK (
                        publication_status IN ('pending', 'published')
                    ),
                    created_at TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS outbox_pending_idx
                    ON outbox(publication_status, created_at);
                """
            )

    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def create_or_get(self, request_key, product_id, quantity):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT {self.ORDER_COLUMNS} FROM orders WHERE request_key = ?",
                (request_key,),
            ).fetchone()
            if existing:
                order = dict(existing)
                if order["product_id"] != product_id or order["quantity"] != quantity:
                    raise RequestKeyConflict("Request key was already used with a different payload")
                return order, False

            order = {
                "order_id": str(uuid4()),
                "request_key": request_key,
                "product_id": product_id,
                "quantity": quantity,
                "unit_price_cents": None,
                "total_cents": None,
                "currency": "MXN",
                "status": "processing",
                "rejection_reason": None,
                "last_error": None,
                "allocation_id": None,
                "allocation_status": None,
                "payment_id": None,
                "payment_status": None,
                "correlation_id": str(uuid4()),
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                f"INSERT INTO orders ({self.ORDER_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(order[column.strip()] for column in self.ORDER_COLUMNS.split(",")),
            )
            return order, True

    def get(self, order_id):
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT {self.ORDER_COLUMNS} FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return self._dict(row)

    def get_by_request_key(self, request_key):
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT {self.ORDER_COLUMNS} FROM orders WHERE request_key = ?",
                (request_key,),
            ).fetchone()
        return self._dict(row)

    def list(self):
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT {self.ORDER_COLUMNS} FROM orders ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def update(self, order_id, **fields):
        allowed = {
            "unit_price_cents", "total_cents", "status", "rejection_reason", "last_error",
            "allocation_id", "allocation_status", "payment_id", "payment_status",
        }
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Invalid order update fields")
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE orders SET {assignments} WHERE order_id = ?",
                (*fields.values(), order_id),
            )
        return self.get(order_id)

    def confirm_and_enqueue(self, order_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {self.ORDER_COLUMNS} FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                return None, None
            order = dict(row)
            if order["status"] != "confirmed" and (
                order["payment_status"] != "approved"
                or order["allocation_status"] != "allocated"
                or order["payment_id"] is None
                or order["allocation_id"] is None
                or order["total_cents"] is None
            ):
                raise ValueError("Only an approved payment with an active allocation can be confirmed")
            existing = connection.execute(
                "SELECT event_id, order_id, event_type, payload, publication_status, "
                "created_at, published_at FROM outbox WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if existing:
                event_record = dict(existing)
                return order, event_record

            event_id = str(uuid4())
            occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            event = {
                "event_id": event_id,
                "event_type": "OrderConfirmed",
                "schema_version": 1,
                "occurred_at": occurred_at,
                "order_id": order["order_id"],
                "product_id": order["product_id"],
                "quantity": order["quantity"],
                "total_cents": order["total_cents"],
                "currency": order["currency"],
                "correlation_id": order["correlation_id"],
            }
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE orders SET status = 'confirmed', last_error = NULL, updated_at = ? "
                "WHERE order_id = ?",
                (now, order_id),
            )
            connection.execute(
                "INSERT INTO outbox "
                "(event_id, order_id, event_type, payload, publication_status, created_at) "
                "VALUES (?, ?, 'OrderConfirmed', ?, 'pending', ?)",
                (event_id, order_id, json.dumps(event, separators=(",", ":")), now),
            )
            order["status"] = "confirmed"
            order["updated_at"] = now
            event_record = {
                "event_id": event_id,
                "order_id": order_id,
                "event_type": "OrderConfirmed",
                "payload": json.dumps(event, separators=(",", ":")),
                "publication_status": "pending",
                "created_at": now,
                "published_at": None,
            }
            return order, event_record

    def get_event(self, event_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT event_id, order_id, event_type, payload, publication_status, "
                "created_at, published_at FROM outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return self._dict(row)

    def get_event_for_order(self, order_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT event_id, order_id, event_type, payload, publication_status, "
                "created_at, published_at FROM outbox WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return self._dict(row)

    def pending_events(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT event_id, order_id, event_type, payload, publication_status, "
                "created_at, published_at FROM outbox WHERE publication_status = 'pending' "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_published(self, event_id):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                "UPDATE outbox SET publication_status = 'published', published_at = ? "
                "WHERE event_id = ? AND publication_status = 'pending'",
                (now, event_id),
            )
        return self.get_event(event_id)
