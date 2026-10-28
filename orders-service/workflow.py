import json
import logging
import os

import httpx
from dotenv import load_dotenv

from store import OrdersStore

load_dotenv()

logger = logging.getLogger("orders.workflow")


class TemporaryFailure(Exception):
    pass


class BusinessRejection(Exception):
    def __init__(self, reason, status_code, message):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


class CheckoutCoordinator:
    def __init__(
        self,
        store,
        products_url=None,
        payments_url=None,
        payment_scenario=None,
        timeout=None,
        publisher=None,
    ):
        self.store = store
        self.products_url = (products_url or os.getenv(
            "PRODUCTS_URL", "http://127.0.0.1:8001"
        )).rstrip("/")
        self.payments_url = (payments_url or os.getenv(
            "PAYMENTS_URL", "http://127.0.0.1:8004"
        )).rstrip("/")
        self.payment_scenario = payment_scenario or os.getenv(
            "PAYMENT_SCENARIO", "approve"
        )
        self.service_token = os.getenv("SERVICE_TOKEN", "")
        self.timeout = timeout or float(os.getenv("SERVICE_HTTP_TIMEOUT_SECONDS", "2.0"))
        self.payment_timeout = float(os.getenv("PAYMENT_HTTP_TIMEOUT_SECONDS", "2.0"))
        self.publisher = publisher

    def _get_product(self, order):
        try:
            response = httpx.get(
                f"{self.products_url}/products/{order['product_id']}",
                timeout=self.timeout, trust_env=False,
            )
        except httpx.RequestError as error:
            raise TemporaryFailure(f"Products lookup outcome unknown: {type(error).__name__}")
        if response.status_code == 404:
            raise BusinessRejection("product_not_found", 404, "Product not found")
        if response.status_code != 200:
            raise TemporaryFailure(f"Products returned HTTP {response.status_code}")
        try:
            product = response.json()
            price = product["price_cents"]
            if (type(price) is not int or price < 0 or product.get("currency") != "MXN"
                    or type(product.get("stock")) is not int or product["stock"] < 0):
                raise ValueError("invalid price, currency, or stock")
            return product
        except (ValueError, KeyError, TypeError) as error:
            raise TemporaryFailure(f"Invalid Products response: {error}")

    def _get_allocation(self, order_id):
        try:
            response = httpx.get(
                f"{self.products_url}/allocations/{order_id}",
                timeout=self.timeout, trust_env=False,
            )
        except httpx.RequestError as error:
            raise TemporaryFailure(f"Products allocation lookup outcome unknown: {type(error).__name__}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TemporaryFailure(f"Products allocation lookup returned HTTP {response.status_code}")
        try:
            allocation = response.json()
            if (allocation["order_id"] != order_id
                    or not allocation.get("correlation_id")
                    or allocation["status"] not in {"allocated", "released"}):
                raise ValueError("allocation identity or status mismatch")
            return allocation
        except (ValueError, KeyError, TypeError) as error:
            raise TemporaryFailure(f"Invalid allocation response: {error}")

    def _obtain_allocation(self, order):
        allocation = self._get_allocation(order["order_id"])
        if allocation is not None:
            if (allocation["product_id"] != order["product_id"]
                    or allocation["quantity"] != order["quantity"]):
                raise TemporaryFailure("Saved allocation does not match order")
            if allocation["correlation_id"] != order["correlation_id"]:
                raise TemporaryFailure("Saved allocation correlation does not match order")
            if allocation["status"] != "allocated":
                raise BusinessRejection("allocation_released", 409, "Stock allocation was already released")
            return allocation

        payload = {
            "order_id": order["order_id"],
            "product_id": order["product_id"],
            "quantity": order["quantity"],
            "correlation_id": order["correlation_id"],
        }
        headers = {"X-Service-Token": self.service_token}
        try:
            response = httpx.post(
                f"{self.products_url}/allocations", json=payload,
                headers=headers, timeout=self.timeout, trust_env=False,
            )
        except httpx.RequestError as error:
            try:
                saved = self._get_allocation(order["order_id"])
            except TemporaryFailure:
                raise TemporaryFailure(f"Allocation result unknown: {type(error).__name__}")
            if saved is not None:
                if (saved["product_id"] == order["product_id"]
                        and saved["quantity"] == order["quantity"]
                        and saved["correlation_id"] == order["correlation_id"]
                        and saved["status"] == "allocated"):
                    return saved
                raise TemporaryFailure("Saved allocation does not match order")
            raise TemporaryFailure(f"Allocation result unknown: {type(error).__name__}")

        if response.status_code == 409:
            raise BusinessRejection("insufficient_stock", 409, "Insufficient stock")
        if response.status_code == 404:
            raise BusinessRejection("product_not_found", 404, "Product not found")
        if response.status_code not in {200, 201}:
            raise TemporaryFailure(f"Products allocation returned HTTP {response.status_code}")
        try:
            allocation = response.json()
            if (allocation["order_id"] != order["order_id"]
                    or allocation["product_id"] != order["product_id"]
                    or allocation["quantity"] != order["quantity"]
                    or allocation["correlation_id"] != order["correlation_id"]
                    or allocation["status"] != "allocated"):
                raise ValueError("allocation does not match order")
            return allocation
        except (ValueError, KeyError, TypeError) as error:
            raise TemporaryFailure(f"Invalid allocation response: {error}")

    def _get_payment(self, order):
        try:
            response = httpx.get(
                f"{self.payments_url}/payments/{order['order_id']}",
                timeout=self.payment_timeout, trust_env=False,
            )
        except httpx.RequestError as error:
            raise TemporaryFailure(f"Payments lookup outcome unknown: {type(error).__name__}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TemporaryFailure(f"Payments lookup returned HTTP {response.status_code}")
        return self._validate_payment(response, order)

    @staticmethod
    def _validate_payment(response, order):
        try:
            payment = response.json()
            if (payment["order_id"] != order["order_id"]
                    or type(payment["amount_cents"]) is not int
                    or payment["currency"] != "MXN"
                    or payment["correlation_id"] != order["correlation_id"]
                    or payment["status"] not in {"approved", "declined"}):
                raise ValueError("payment identity, amount, currency, or status mismatch")
            return payment
        except (ValueError, KeyError, TypeError) as error:
            raise TemporaryFailure(f"Invalid Payments response: {error}")

    def _obtain_payment(self, order):
        payment = self._get_payment(order)
        if payment is not None:
            if payment["amount_cents"] != order["total_cents"]:
                raise TemporaryFailure("Saved payment amount does not match order")
            return payment

        payload = {
            "order_id": order["order_id"],
            "amount_cents": order["total_cents"],
            "currency": "MXN",
            "correlation_id": order["correlation_id"],
            "scenario": self.payment_scenario,
        }
        headers = {"X-Service-Token": self.service_token}
        try:
            response = httpx.post(
                f"{self.payments_url}/payments", json=payload,
                headers=headers, timeout=self.payment_timeout, trust_env=False,
            )
        except httpx.RequestError as error:
            raise TemporaryFailure(f"Payment result unknown: {type(error).__name__}")

        if response.status_code not in {200, 201}:
            raise TemporaryFailure(f"Payments returned HTTP {response.status_code}")
        payment = self._validate_payment(response, order)
        if payment["amount_cents"] != order["total_cents"]:
            raise TemporaryFailure("Payment amount does not match order")
        return payment

    def _release_allocation(self, order_id, correlation_id):
        try:
            response = httpx.post(
                f"{self.products_url}/allocations/{order_id}/release",
                headers={
                    "X-Service-Token": self.service_token,
                    "X-Correlation-ID": correlation_id,
                },
                timeout=self.timeout, trust_env=False,
            )
        except httpx.RequestError:
            response = None
        if response is not None and response.status_code == 200:
            try:
                result = response.json()
                if result["status"] == "released" and result["order_id"] == order_id:
                    return result
            except (ValueError, KeyError, TypeError):
                pass
        try:
            allocation = self._get_allocation(order_id)
        except TemporaryFailure as error:
            raise TemporaryFailure(f"Stock release outcome unknown: {error}")
        if allocation and allocation["status"] == "released":
            return allocation
        raise TemporaryFailure("Stock release has not completed")

    def publish_pending(self, order_id=None):
        if self.publisher is None:
            return 0
        events = self.store.pending_events()
        published = 0
        for event_record in events:
            if order_id and event_record["order_id"] != order_id:
                continue
            event = {}
            try:
                event = json.loads(event_record["payload"])
                self.publisher(event)
            except Exception as error:
                logger.warning(
                    "operation=publish outcome=pending order_id=%s correlation_id=%s "
                    "event_id=%s error=%s",
                    event_record["order_id"], event.get("correlation_id"),
                    event_record["event_id"], type(error).__name__,
                )
                continue
            self.store.mark_published(event_record["event_id"])
            logger.info(
                "operation=publish outcome=published order_id=%s correlation_id=%s event_id=%s",
                event_record["order_id"], event.get("correlation_id"),
                event_record["event_id"],
            )
            published += 1
        return published

    def resume(self, order_id):
        order = self.store.get(order_id)
        if order is None:
            return None, 404
        if order["status"] == "confirmed":
            self.publish_pending(order_id)
            return self.store.get(order_id), 200
        if order["status"] == "rejected":
            return order, 200

        if order["status"] == "compensation_pending" or order["payment_status"] == "declined":
            try:
                allocation = self._release_allocation(
                    order_id, order["correlation_id"],
                )
            except TemporaryFailure as error:
                order = self.store.update(
                    order_id, status="compensation_pending", last_error=str(error),
                )
                return order, 202
            order = self.store.update(
                order_id, status="rejected", rejection_reason="payment_declined",
                allocation_id=allocation["allocation_id"], allocation_status="released",
                last_error=None,
            )
            return order, 402

        try:
            if order["unit_price_cents"] is None:
                product = self._get_product(order)
                order = self.store.update(
                    order_id,
                    unit_price_cents=product["price_cents"],
                    total_cents=product["price_cents"] * order["quantity"],
                    last_error=None,
                )

            if order["allocation_status"] != "allocated":
                allocation = self._obtain_allocation(order)
                order = self.store.update(
                    order_id,
                    allocation_id=allocation["allocation_id"],
                    allocation_status=allocation["status"],
                    last_error=None,
                )

            if order["payment_status"] != "approved":
                payment = self._obtain_payment(order)
                order = self.store.update(
                    order_id,
                    payment_id=payment["payment_id"],
                    payment_status=payment["status"],
                    last_error=None,
                )
                if payment["status"] == "declined":
                    order = self.store.update(
                        order_id, status="compensation_pending", last_error=None,
                    )
                    try:
                        allocation = self._release_allocation(
                            order_id, order["correlation_id"],
                        )
                    except TemporaryFailure as error:
                        order = self.store.update(order_id, last_error=str(error))
                        return order, 202
                    order = self.store.update(
                        order_id, status="rejected", rejection_reason="payment_declined",
                        allocation_id=allocation["allocation_id"],
                        allocation_status="released", last_error=None,
                    )
                    logger.info(
                        "operation=checkout outcome=rejected reason=payment_declined "
                        "order_id=%s correlation_id=%s",
                        order_id, order["correlation_id"],
                    )
                    return order, 402

            order, _event = self.store.confirm_and_enqueue(order_id)
            if order is None:
                return None, 404
            self.publish_pending(order_id)
            order = self.store.get(order_id)
            logger.info(
                "operation=checkout outcome=confirmed order_id=%s correlation_id=%s",
                order_id, order["correlation_id"],
            )
            return order, 200
        except BusinessRejection as rejection:
            order = self.store.update(
                order_id, status="rejected", rejection_reason=rejection.reason,
                last_error=str(rejection),
            )
            logger.info(
                "operation=checkout outcome=rejected reason=%s order_id=%s correlation_id=%s",
                rejection.reason, order_id, order["correlation_id"],
            )
            return order, rejection.status_code
        except TemporaryFailure as error:
            order = self.store.update(
                order_id, status="recovery_required", last_error=str(error),
            )
            logger.warning(
                "operation=checkout outcome=recovery_required order_id=%s "
                "correlation_id=%s error=%s",
                order_id, order["correlation_id"], str(error),
            )
            return order, 202
