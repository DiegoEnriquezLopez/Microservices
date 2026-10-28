import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


class PaymentConflict(Exception):
    pass


class PaymentsStore:
    def __init__(self, db_path="payments.db"):
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
            connection.execute("BEGIN IMMEDIATE")
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'payments'"
            ).fetchone()
            if schema and "timeout_after_commit" not in (schema["sql"] or "").lower():
                connection.execute(
                    "ALTER TABLE payments RENAME TO payments_before_timeout_scenario"
                )
                self._create_payments_table(
                    connection, "payments_with_timeout_scenario",
                )
                connection.execute(
                    "INSERT INTO payments_with_timeout_scenario "
                    "(order_id, payment_id, amount_cents, currency, status, correlation_id, "
                    "scenario, created_at) "
                    "SELECT order_id, payment_id, amount_cents, currency, status, "
                    "correlation_id, scenario, created_at "
                    "FROM payments_before_timeout_scenario"
                )
                connection.execute("DROP TABLE payments_before_timeout_scenario")
                connection.execute(
                    "ALTER TABLE payments_with_timeout_scenario RENAME TO payments"
                )
            elif not schema:
                self._create_payments_table(connection)

    @staticmethod
    def _create_payments_table(connection, table_name="payments"):
        connection.execute(
            f"""
            CREATE TABLE {table_name} (
                order_id TEXT PRIMARY KEY,
                payment_id TEXT NOT NULL UNIQUE,
                amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                currency TEXT NOT NULL CHECK (currency = 'MXN'),
                status TEXT NOT NULL CHECK (status IN ('approved', 'declined')),
                correlation_id TEXT NOT NULL,
                scenario TEXT NOT NULL CHECK (
                    scenario IN ('approve', 'decline', 'timeout_after_commit')
                ),
                created_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def get(self, order_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT order_id, payment_id, amount_cents, currency, status, "
                "correlation_id, scenario, created_at FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return self._dict(row)

    def list(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT order_id, payment_id, amount_cents, currency, status, "
                "correlation_id, scenario, created_at FROM payments ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def create(self, order_id, amount_cents, currency, scenario, correlation_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT order_id, payment_id, amount_cents, currency, status, "
                "correlation_id, scenario, created_at FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if existing:
                payment = dict(existing)
                if (payment["amount_cents"] != amount_cents
                        or payment["currency"] != currency
                        or payment["correlation_id"] != correlation_id
                        or payment["scenario"] != scenario):
                    raise PaymentConflict("Conflicting payment payload for order")
                return payment, False

            status = "declined" if scenario == "decline" else "approved"
            payment = {
                "order_id": order_id,
                "payment_id": str(uuid4()),
                "amount_cents": amount_cents,
                "currency": currency,
                "status": status,
                "correlation_id": correlation_id,
                "scenario": scenario,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            connection.execute(
                "INSERT INTO payments "
                "(order_id, payment_id, amount_cents, currency, status, correlation_id, "
                "scenario, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(payment.values()),
            )
            return payment, True
