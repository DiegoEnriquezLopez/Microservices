import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


SEED_PRODUCTS = (
    (1, "Keyboard", 50000, "MXN", 15),
    (2, "Mouse", 15000, "MXN", 30),
    (3, "Monitor", 349900, "MXN", 8),
)


class ProductNotFound(Exception):
    pass


class InsufficientStock(Exception):
    pass


class AllocationConflict(Exception):
    pass


class ProductsStore:
    def __init__(self, db_path="products.db", seed=True):
        self.db_path = str(db_path)
        self.initialize(seed=seed)

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

    def initialize(self, seed=True):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    product_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                    currency TEXT NOT NULL CHECK (currency = 'MXN'),
                    stock INTEGER NOT NULL CHECK (stock >= 0)
                );
                CREATE TABLE IF NOT EXISTS allocations (
                    order_id TEXT PRIMARY KEY,
                    allocation_id TEXT NOT NULL UNIQUE,
                    product_id INTEGER NOT NULL REFERENCES products(product_id),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    correlation_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('allocated', 'released')),
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                """
            )
            if seed:
                connection.executemany(
                    "INSERT OR IGNORE INTO products "
                    "(product_id, name, price_cents, currency, stock) VALUES (?, ?, ?, ?, ?)",
                    SEED_PRODUCTS,
                )

    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def list_products(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT product_id AS id, name, price_cents, currency, stock "
                "FROM products ORDER BY product_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_product(self, product_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT product_id AS id, name, price_cents, currency, stock "
                "FROM products WHERE product_id = ?", (product_id,),
            ).fetchone()
        return self._dict(row)

    def get_allocation(self, order_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT order_id, allocation_id, product_id, quantity, correlation_id, status, "
                "created_at, released_at FROM allocations WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return self._dict(row)

    def allocate(self, order_id, product_id, quantity, correlation_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT order_id, allocation_id, product_id, quantity, correlation_id, status, "
                "created_at, released_at FROM allocations WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if existing:
                allocation = dict(existing)
                if (allocation["product_id"] != product_id
                        or allocation["quantity"] != quantity
                        or allocation["correlation_id"] != correlation_id
                        or allocation["status"] != "allocated"):
                    raise AllocationConflict("Order already has a different or released allocation")
                return allocation, False

            updated = connection.execute(
                "UPDATE products SET stock = stock - ? "
                "WHERE product_id = ? AND stock >= ?",
                (quantity, product_id, quantity),
            )
            if updated.rowcount != 1:
                product = connection.execute(
                    "SELECT 1 FROM products WHERE product_id = ?", (product_id,),
                ).fetchone()
                if not product:
                    raise ProductNotFound(product_id)
                raise InsufficientStock(product_id)

            allocation = {
                "order_id": order_id,
                "allocation_id": str(uuid4()),
                "product_id": product_id,
                "quantity": quantity,
                "correlation_id": correlation_id,
                "status": "allocated",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "released_at": None,
            }
            connection.execute(
                "INSERT INTO allocations "
                "(order_id, allocation_id, product_id, quantity, correlation_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'allocated', ?)",
                (order_id, allocation["allocation_id"], product_id, quantity,
                 correlation_id,
                 allocation["created_at"]),
            )
            return allocation, True

    def release(self, order_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT order_id, allocation_id, product_id, quantity, correlation_id, status, "
                "created_at, released_at FROM allocations WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not row:
                return None
            allocation = dict(row)
            if allocation["status"] == "allocated":
                released_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "UPDATE products SET stock = stock + ? WHERE product_id = ?",
                    (allocation["quantity"], allocation["product_id"]),
                )
                connection.execute(
                    "UPDATE allocations SET status = 'released', released_at = ? "
                    "WHERE order_id = ? AND status = 'allocated'",
                    (released_at, order_id),
                )
                allocation["status"] = "released"
                allocation["released_at"] = released_at
            return allocation

