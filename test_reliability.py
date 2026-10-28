import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

PRODUCTS_STORE_MODULE = _load_module(
    "products_store_for_tests", ROOT / "products-service" / "store.py",
)
_products_import_dir = tempfile.TemporaryDirectory(prefix="products-test-import-")
_old_products_db_path = os.environ.get("PRODUCTS_DB_PATH")
os.environ["PRODUCTS_DB_PATH"] = str(Path(_products_import_dir.name) / "unused.db")
sys.modules["store"] = PRODUCTS_STORE_MODULE
try:
    PRODUCTS_API_MODULE = _load_module(
        "products_api_for_tests", ROOT / "products-service" / "main.py",
    )
finally:
    sys.modules.pop("store", None)
    if _old_products_db_path is None:
        os.environ.pop("PRODUCTS_DB_PATH", None)
    else:
        os.environ["PRODUCTS_DB_PATH"] = _old_products_db_path

ORDERS_STORE_MODULE = _load_module(
    "orders_store_for_tests", ROOT / "orders-service" / "store.py",
)
sys.modules["store"] = ORDERS_STORE_MODULE
try:
    ORDERS_WORKFLOW_MODULE = _load_module(
        "orders_workflow_for_tests", ROOT / "orders-service" / "workflow.py",
    )
finally:
    sys.modules.pop("store", None)

PAYMENTS_STORE_MODULE = _load_module(
    "payments_store_for_tests", ROOT / "payments-service" / "store.py",
)

_orders_import_dir = tempfile.TemporaryDirectory(prefix="orders-test-import-")
_old_orders_db_path = os.environ.get("ORDERS_DB_PATH")
os.environ["ORDERS_DB_PATH"] = str(Path(_orders_import_dir.name) / "unused.db")
sys.modules["store"] = ORDERS_STORE_MODULE
sys.modules["workflow"] = ORDERS_WORKFLOW_MODULE
try:
    ORDERS_API_MODULE = _load_module(
        "orders_api_for_tests", ROOT / "orders-service" / "main.py",
    )
finally:
    sys.modules.pop("store", None)
    sys.modules.pop("workflow", None)
    if _old_orders_db_path is None:
        os.environ.pop("ORDERS_DB_PATH", None)
    else:
        os.environ["ORDERS_DB_PATH"] = _old_orders_db_path

from notification_consumer import NotificationsStore  # noqa: E402


class CompetingStockRequestsTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_concurrent_requests_compete_for_the_last_unit(self):
        with tempfile.TemporaryDirectory(prefix="products-concurrency-test-") as directory:
            store = PRODUCTS_STORE_MODULE.ProductsStore(
                Path(directory) / "products.db", seed=False,
            )
            with store.connect() as connection:
                connection.execute(
                    "INSERT INTO products "
                    "(product_id, name, price_cents, currency, stock) "
                    "VALUES (1, 'Test item', 1250, 'MXN', 1)"
                )

            app = PRODUCTS_API_MODULE.create_app(store)
            transport = httpx.ASGITransport(app=app)
            waiting = 0
            both_requests_ready = asyncio.Event()

            async def allocate(client, order_id):
                nonlocal waiting
                waiting += 1
                if waiting == 2:
                    both_requests_ready.set()
                await both_requests_ready.wait()
                return await client.post(
                    "/allocations",
                    headers={"X-Service-Token": "reliability-test-token"},
                    json={
                        "order_id": order_id,
                        "product_id": 1,
                        "quantity": 1,
                        "correlation_id": f"trace-{order_id}",
                    },
                )

            with patch.dict(os.environ, {"SERVICE_TOKEN": "reliability-test-token"}):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://products.test",
                ) as client:
                    responses = await asyncio.gather(
                        allocate(client, "concurrent-order-a"),
                        allocate(client, "concurrent-order-b"),
                    )

            self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
            self.assertEqual(store.get_product(1)["stock"], 0)
            with store.connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM allocations").fetchone()[0]
            self.assertEqual(count, 1)


class CheckoutReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="checkout-reliability-test-")
        directory = Path(self.temp_dir.name)
        self.products = PRODUCTS_STORE_MODULE.ProductsStore(
            directory / "products.db", seed=False,
        )
        with self.products.connect() as connection:
            connection.execute(
                "INSERT INTO products "
                "(product_id, name, price_cents, currency, stock) "
                "VALUES (1, 'Test item', 5000, 'MXN', 10)"
            )
        self.orders_db = directory / "orders.db"
        self.orders = ORDERS_STORE_MODULE.OrdersStore(self.orders_db)
        self.payments = PAYMENTS_STORE_MODULE.PaymentsStore(directory / "payments.db")
        self.products_url = "http://products.test"
        self.payments_url = "http://payments.test"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _response(status_code, payload):
        return httpx.Response(status_code, json=payload)

    def _fake_get(self, url, **_kwargs):
        parsed = urlparse(url)
        if parsed.netloc == "products.test":
            if parsed.path.startswith("/products/"):
                product_id = int(parsed.path.rsplit("/", 1)[1])
                product = self.products.get_product(product_id)
                return self._response(200, product) if product else self._response(404, {})
            if parsed.path.startswith("/allocations/"):
                order_id = parsed.path.rsplit("/", 1)[1]
                allocation = self.products.get_allocation(order_id)
                return self._response(200, allocation) if allocation else self._response(404, {})
        if parsed.netloc == "payments.test" and parsed.path.startswith("/payments/"):
            order_id = parsed.path.rsplit("/", 1)[1]
            payment = self.payments.get(order_id)
            return self._response(200, payment) if payment else self._response(404, {})
        self.fail(f"Unexpected service GET in test: {url}")

    def _fake_post(self, url, **kwargs):
        parsed = urlparse(url)
        payload = kwargs.get("json", {})
        if parsed.netloc == "products.test" and parsed.path == "/allocations":
            try:
                allocation, created = self.products.allocate(
                    payload["order_id"], payload["product_id"], payload["quantity"],
                    payload["correlation_id"],
                )
            except PRODUCTS_STORE_MODULE.InsufficientStock:
                return self._response(409, {"detail": "Insufficient stock"})
            return self._response(201 if created else 200, allocation)
        if parsed.netloc == "products.test" and parsed.path.endswith("/release"):
            order_id = parsed.path.split("/")[-2]
            allocation = self.products.release(order_id)
            return self._response(200, allocation) if allocation else self._response(404, {})
        if parsed.netloc == "payments.test" and parsed.path == "/payments":
            payment, created = self.payments.create(
                payload["order_id"], payload["amount_cents"], payload["currency"],
                payload["scenario"], payload["correlation_id"],
            )
            return self._response(201 if created else 200, payment)
        self.fail(f"Unexpected service POST in test: {url}")

    def _patched_service_calls(self):
        return (
            patch.object(ORDERS_WORKFLOW_MODULE.httpx, "get", side_effect=self._fake_get),
            patch.object(ORDERS_WORKFLOW_MODULE.httpx, "post", side_effect=self._fake_post),
        )

    def _coordinator(self, scenario="approve", store=None):
        return ORDERS_WORKFLOW_MODULE.CheckoutCoordinator(
            store or self.orders,
            products_url=self.products_url,
            payments_url=self.payments_url,
            payment_scenario=scenario,
            timeout=1,
            publisher=None,
        )

    def test_duplicate_checkout_reuses_persisted_order_without_repeating_effects(self):
        request_key = "automated-duplicate-checkout"
        stock_before = self.products.get_product(1)["stock"]
        coordinator = self._coordinator()
        get_patch, post_patch = self._patched_service_calls()

        with get_patch, post_patch:
            first_app = ORDERS_API_MODULE.create_app(self.orders, coordinator)
            with TestClient(first_app) as client:
                first_response = client.post(
                    "/orders",
                    json={"request_key": request_key, "product_id": 1, "quantity": 2},
                )
            self.assertEqual(first_response.status_code, 201)
            first_result = first_response.json()
            self.assertEqual(first_result["status"], "confirmed")

            reopened_orders = ORDERS_STORE_MODULE.OrdersStore(self.orders_db)
            second_app = ORDERS_API_MODULE.create_app(
                reopened_orders, self._coordinator(store=reopened_orders),
            )
            with TestClient(second_app) as client:
                repeated_response = client.post(
                    "/orders",
                    json={"request_key": request_key, "product_id": 1, "quantity": 2},
                )
                conflict_response = client.post(
                    "/orders",
                    json={"request_key": request_key, "product_id": 1, "quantity": 1},
                )
            self.assertEqual(repeated_response.status_code, 200)
            second_result = repeated_response.json()
            self.assertEqual(second_result["status"], "confirmed")
            self.assertEqual(second_result["order_id"], first_result["order_id"])
            self.assertEqual(conflict_response.status_code, 409)
            self.assertEqual(conflict_response.json()["order_id"], first_result["order_id"])

        self.assertEqual(self.products.get_product(1)["stock"], stock_before - 2)
        self.assertEqual(len(self.payments.list()), 1)
        order_id = first_result["order_id"]
        original_event = self.orders.get_event_for_order(order_id)
        reopened_event = reopened_orders.get_event_for_order(order_id)
        self.assertEqual(reopened_event["event_id"], original_event["event_id"])

    def test_declined_payment_releases_stock_once_and_creates_no_event(self):
        order, _created = self.orders.create_or_get(
            "automated-payment-decline", product_id=1, quantity=3,
        )
        stock_before = self.products.get_product(1)["stock"]
        coordinator = self._coordinator(scenario="decline")
        get_patch, post_patch = self._patched_service_calls()

        with get_patch, post_patch:
            rejected, status_code = coordinator.resume(order["order_id"])
            self.assertEqual(status_code, 402)
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["rejection_reason"], "payment_declined")
            self.assertEqual(rejected["payment_status"], "declined")
            self.assertEqual(rejected["allocation_status"], "released")
            allocation_id = rejected["allocation_id"]
            payment_id = rejected["payment_id"]

            # Repeating recovery must return the saved rejection without releasing again.
            repeated, repeated_code = coordinator.resume(order["order_id"])
            self.assertEqual(repeated_code, 200)
            self.assertEqual(repeated["allocation_id"], allocation_id)
            self.assertEqual(repeated["payment_id"], payment_id)

        self.assertEqual(self.products.get_product(1)["stock"], stock_before)
        self.assertEqual(self.products.get_allocation(order["order_id"])["status"], "released")
        self.assertEqual(len(self.payments.list()), 1)
        self.assertIsNone(self.orders.get_event_for_order(order["order_id"]))


class NotificationDeduplicationTest(unittest.TestCase):
    def test_replayed_event_after_reopening_database_stays_deduplicated(self):
        with tempfile.TemporaryDirectory(prefix="notifications-reliability-test-") as directory:
            db_path = Path(directory) / "notifications.db"
            event = {
                "event_id": "automated-event-001",
                "event_type": "OrderConfirmed",
                "schema_version": 1,
                "occurred_at": "2026-10-21T12:00:00Z",
                "order_id": "automated-order-001",
                "product_id": 1,
                "quantity": 2,
                "total_cents": 10000,
                "currency": "MXN",
                "correlation_id": "automated-trace-001",
            }

            first_store = NotificationsStore(db_path)
            first_outcome, _confirmation = first_store.process_event(event)
            self.assertEqual(first_outcome, "created")

            reopened_store = NotificationsStore(db_path)
            second_outcome, confirmation = reopened_store.process_event(event)
            self.assertEqual(second_outcome, "duplicate")
            self.assertEqual(confirmation["event_id"], event["event_id"])
            self.assertEqual(len(reopened_store.list_confirmations()), 1)
            self.assertEqual(len(reopened_store.list_processed_event_ids()), 1)


if __name__ == "__main__":
    unittest.main()
