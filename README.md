# Distributed Store

A local distributed-systems project that simulates an online purchasing workflow using independent Python microservices. The application coordinates product inventory, simulated payments, order processing, and asynchronous notifications.

## Products Service

Displays the product catalog, inventory information, and product-related operations.

<img width="713" height="394" alt="image" src="https://github.com/user-attachments/assets/c8e67b3f-2372-437d-bebc-a10f6d69cd01" />

## Orders Service

Demonstrates order creation and the responses returned by the service during the purchase workflow.

<img width="713" height="422" alt="image" src="https://github.com/user-attachments/assets/21dc3932-a887-4774-8960-6ef78bea7d99" />

## Payments Service

Shows the simulated payment processing operations and their responses.

<img width="713" height="342" alt="image" src="https://github.com/user-attachments/assets/65da55db-9a5b-4021-8c36-e81faa38db9d" />

## Notifications Service

Shows the processing of order confirmation events through RabbitMQ in the terminal.

## Technologies

- **Python** — application logic and service implementation.
- **FastAPI** — REST APIs for Products, Orders, and Payments.
- **SQLite** — independent persistent databases for each service.
- **RabbitMQ** — asynchronous messaging and event delivery.
- **Docker and Docker Compose** — broker deployment and containerized service testing.
- **unittest** — automated reliability tests.

## Architecture

The system consists of four components:

| Component | Responsibility | Database |
|---|---|---|
| Products | Product catalog, stock allocation, and stock release | `products-service/products.db` |
| Orders | Purchase coordination, idempotency, recovery, and transactional outbox | `orders-service/orders.db` |
| Payments | Simulated payment approval and decline, with persistent results | `payments-service/payments.db` |
| Notifications | Asynchronous consumption and deduplication of order confirmation events | `notifications.db` |

Orders coordinates purchases through HTTP requests and publishes `OrderConfirmed` events through RabbitMQ. Notifications consumes these events asynchronously and persists confirmations in its own SQLite database.

The project implements reliability mechanisms such as idempotent requests, transactional stock updates, payment recovery, compensation, transactional outbox, publisher confirms, and duplicate-event detection.

Products owns both the catalog and inventory so that one service controls availability, allocation, and release. Orders requests these changes from Products over HTTP and does not directly access the Products or Payments databases. It calls their APIs and publishes the event consumed by Notifications.

Notifications runs as a worker without an HTTP server. RabbitMQ transports events, while Orders coordinates the business workflow. `broker.py` contains Orders' RabbitMQ integration; it is not an additional business service.

Prices are stored as integer amounts in MXN cents.

## Start in PowerShell

**Working directory:** the project root, `distributed-store-practice-3`.

### Group 1 — Create the local configuration

Run this line once from the project root:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

The `.env.example` file contains the example configuration. Review `.env` and change the local token if desired.

### Group 2 — Prepare Python

If the virtual environment does not exist yet, run both lines together in the same terminal:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Group 3 — Start the broker

Start RabbitMQ and declare the exchange, queue, and binding:

```powershell
docker compose up -d rabbitmq
.\.venv\Scripts\python.exe setup_broker.py
```

### Group 4 — Start the services

Open four separate terminals in the project root. Run one command in each terminal and leave each process running. Do not copy all four commands into a single terminal.

**Products:**

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir products-service --port 8001
```

**Payments:**

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir payments-service --port 8004
```

**Orders:**

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir orders-service --port 8002
```

**Notifications:**

```powershell
.\.venv\Scripts\python.exe notification_consumer.py
```

The services create their databases and the initial catalog when they start.

`.env.example` lists the URLs, database paths, timeouts, local RabbitMQ credentials, and shared token. The same `SERVICE_TOKEN` is loaded by Products, Payments, and Orders. The example values are for local demonstration only; do not store real credentials in the code or publish them to GitHub. Notifications does not need the token.

When running directly on the computer, the service and RabbitMQ URLs use `127.0.0.1`. If processes run in containers on the same Compose network, configure their URLs with the service names, for example:

```text
http://products:8001
http://payments:8004
RABBITMQ_HOST=rabbitmq
```

## Run Products in Docker with a persistent database

**Working directory:** the project root.

The container exposes Products on local port `8011`, separate from port `8001` used by the local Python process. The published port is bound to loopback.

### Group 5 — Build and start Products in Docker

Run all three commands together in the same terminal:

```powershell
docker build -f products-service/Dockerfile -t distributed-store-products:local .
docker volume create products_data
docker run --rm -d --name products-api -p 127.0.0.1:8011:8001 --env-file .env -e PRODUCTS_DB_PATH=/data/products.db -v products_data:/data distributed-store-products:local
```

### Group 6 — Query and reserve one unit

Run these commands together in the same PowerShell terminal. The token must match the local token configured in `.env`.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8011/health"
Invoke-RestMethod -Uri "http://127.0.0.1:8011/products"

$headers = @{ "X-Service-Token" = "local-demo-token" }
$body = '{"order_id":"docker-volume-demo-001","product_id":1,"quantity":1,"correlation_id":"docker-volume-demo-001"}'

Invoke-RestMethod -Uri "http://127.0.0.1:8011/allocations" -Method Post -Headers $headers -ContentType "application/json" -Body $body

docker stop products-api
```

### Group 7 — Check that the volume retains the data

After stopping the previous container, run these commands together:

```powershell
docker run --rm -d --name products-api -p 127.0.0.1:8011:8001 --env-file .env -e PRODUCTS_DB_PATH=/data/products.db -v products_data:/data distributed-store-products:local

Invoke-RestMethod -Uri "http://127.0.0.1:8011/allocations/docker-volume-demo-001"
Invoke-RestMethod -Uri "http://127.0.0.1:8011/products/1"

docker stop products-api
```

The allocation and reduced stock should still appear after recreating the container. The `products_data` volume retains `/data/products.db`.

## Local payment scenarios

Test scenarios are disabled by default in Payments. They are accepted only when `APP_ENV=local` and `PAYMENT_TEST_CONTROLS_ENABLED=true`, as configured in `.env.example`. In other environments, Payments returns HTTP 403 for `decline` and `timeout_after_commit`.

The normal payment scenario is `approve`.

### Group 8 — Start Orders to test a declined payment

Set `PAYMENT_SCENARIO=decline` in the Orders terminal before starting it. From the project root, run both lines together in a new terminal:

```powershell
$env:PAYMENT_SCENARIO = "decline"
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir orders-service --port 8002
```

To test a lost response after the payment is committed, replace `decline` with `timeout_after_commit`:

```powershell
$env:PAYMENT_SCENARIO = "timeout_after_commit"
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir orders-service --port 8002
```

Payments saves the approved result and then delays the response for at least half a second longer than `PAYMENT_HTTP_TIMEOUT_SECONDS` (three seconds by default versus a two-second timeout).

Keep both processes on the common timeout configured in `.env`.

Orders should respond with HTTP 202 and a recovery state. Query `GET /payments/{order_id}` to see the approved payment and resume the same order with `POST /orders/{order_id}/resume`.

The query or retry returns the persisted result without creating another payment.

## Make a purchase

The client sends only `request_key`, `product_id`, and `quantity`. It does not send the price or total. `request_key` identifies the same purchase when the client retries the request with the same payload.

### HTTP orchestration

1. The client calls Orders.
2. Orders saves the request.
3. Orders queries the current price from Products and calculates and saves the total.
4. Orders asks Products to allocate the stock.
5. Orders requests a simulated payment result from Payments using `order_id` as a stable identifier.
6. If payment is approved and stock remains allocated, Orders confirms the purchase and creates an event for Notifications.

Products reduces available stock when it creates the allocation.

### Event choreography

When Payments approves and stock is allocated, Orders marks the purchase as confirmed and saves `OrderConfirmed` to its outbox in the same SQLite transaction. It then publishes pending events to RabbitMQ.

Notifications consumes the event and records the confirmation and processed event ID before acknowledging the message.

Orders does not call Notifications directly. RabbitMQ transports the event, and Orders determines the business sequence. Docker Compose starts RabbitMQ but does not coordinate the purchase.

The initial catalog contains:

| Product | Price (MXN) | Initial stock |
|---|---:|---:|
| Keyboard | $500.00 | 15 |
| Mouse | $150.00 | 30 |
| Monitor | $3,499.00 | 8 |

Query the catalog at `http://127.0.0.1:8001/products`. Use a new key for each purchase.

### Group 9 — Create a sample purchase

Run these commands together in a PowerShell terminal:

```powershell
$body = '{"request_key":"compra-001","product_id":1,"quantity":2}'

$order = Invoke-RestMethod -Uri "http://127.0.0.1:8002/orders" -Method Post -ContentType "application/json" -Body $body

$order | ConvertTo-Json -Depth 5
```

Query the status with `GET /orders/{order_id}`.

After the event is processed, the worker records the confirmation in `notifications.db`.

### Group 10 — List confirmations

Run this command from the project root. If the worker is running in another terminal, leave it open while you run this command:

```powershell
.\.venv\Scripts\python.exe notification_consumer.py --list-confirmations
```

To retry a pending purchase, use:

```text
POST /orders/{order_id}/resume
```

To retry pending events, use:

```text
POST /events/publish-pending
```

The Products, Payments, and Orders APIs provide Swagger documentation at `/docs`.

## HTTP and event contracts

JSON request bodies do not accept extra fields. Amounts are integers in MXN cents; quantities and product IDs are positive integers.

FastAPI returns HTTP 422 with validation details when a required field is missing, a field has an incompatible type, or the request body does not meet the validation rules.

Errors raised by validation or `HTTPException` include `detail`. An Orders request-key conflict also returns the saved `order_id` and status. An internal error 500 has no additional business schema.

### Products (`127.0.0.1:8001`)

| Operation and purpose | Input | Success | Relevant errors |
|---|---|---|---|
| `GET /products` — list the catalog | No body | 200: list with `id`, `name`, `price_cents`, `currency`, and `stock` | 500 if the local database cannot be queried |
| `GET /products/{id}` — query price and availability | ID in path | 200: product | 404 if it does not exist; 422 if the ID is not a valid integer |
| `POST /allocations` — reserve units for an order | `order_id`, `product_id`, `quantity`, `correlation_id`; `X-Service-Token` header | 201 when created; 200 when the same active allocation is repeated. Returns allocation data with status `allocated` | 401 token missing or incorrect; 503 token not configured or database busy (includes `Retry-After: 1`); 404 product not found; 409 insufficient stock or incompatible existing allocation; 422 invalid body |
| `GET /allocations/{order_id}` — query the allocation | Order ID in path | 200: allocation and its status | 404 if it does not exist |
| `POST /allocations/{order_id}/release` — release an allocation | Order ID in path; `X-Service-Token` header; `X-Correlation-ID` for internal tracing | 200: allocation with status `released`; repeating the release returns the same result without adding stock again | 401 token missing or incorrect; 503 token not configured or database busy (includes `Retry-After: 1`); 404 if no allocation exists |

A product returns `id`, `name`, `price_cents`, `currency`, and `stock`.

An allocation returns `order_id`, `allocation_id`, `product_id`, `quantity`, `correlation_id`, `status`, `created_at`, and `released_at`.

### Payments (`127.0.0.1:8004`)

| Operation and purpose | Input | Success | Relevant errors |
|---|---|---|---|
| `POST /payments` — save or retrieve the simulated result | `order_id`, `amount_cents`, `currency: MXN`, `correlation_id`, `scenario` (`approve`, `decline`, or `timeout_after_commit`, optional and defaults to `approve`); `X-Service-Token` header | 201 when a result is saved; 200 when the exact operation is repeated. Returns payment ID, amount, currency, status (`approved` or `declined`), and correlation data | 401 token missing or incorrect; 403 test scenario outside local mode; 503 token not configured or database busy (includes `Retry-After: 1`); 409 same `order_id` with a different amount, currency, scenario, or correlation; 422 invalid body |
| `GET /payments/{order_id}` — query the result | Order ID in path | 200: persisted result | 404 if there is no result yet |
| `GET /payments` — inspect local results | No body | 200: list of results for local inspection | 500 if the local database cannot be queried |

`scenario` is a test control to cause approval or decline; it does not represent a real payment provider.

Orders sends the same `order_id` when retrying, and Payments keeps one result per order. A payment response contains `order_id`, `payment_id`, `amount_cents`, `currency`, `status`, `correlation_id`, `scenario`, and `created_at`.

### Orders (`127.0.0.1:8002`)

| Operation and purpose | Input | Success | Rejections and relevant errors |
|---|---|---|---|
| `POST /orders` — create or retry an idempotent checkout | `request_key`, `product_id`, `quantity` | 201 for a newly completed purchase; 200 when a completed purchase is repeated; 202 if recovery work remains. The response includes the saved order ID and status when an order was created; it also reports price/total if already obtained and event publication status | 422 invalid request; 409 insufficient stock or request key reused with a different payload; 404 product not found; 402 payment declined once detected and stock released; 202 dependency temporarily unavailable, without claiming payment was declined; 503 Orders database busy before saving the order. A key conflict includes the existing order ID and status |
| `GET /orders/{order_id}` — query order progress | Order ID in path | 200: saved order, including status, order ID, progress, and event ID/publication status if an event exists | 404 if it does not exist |
| `GET /orders` — inspect local orders | No body | 200: list of orders and their progress | 500 if the local database cannot be queried |
| `POST /orders/{order_id}/resume` — resume recovery or compensation | Order ID in path; no body | 200 when completed or when showing an already saved terminal state; 202 if recovery or compensation is still pending | 404 if it does not exist; 402 when this attempt detects the payment decline and completes stock release; 202 if the result cannot temporarily be determined or completed |
| `POST /events/publish-pending` — retry outbox publications | No body | 200: `published_count`, `pending_count`, and the IDs still pending | A RabbitMQ failure leaves the event pending and appears in the summary; it does not undo the confirmed purchase |
| `POST /events/{event_id}/replay` — republish an existing event | Event ID in path | 200: record for the event that was republished | 404 if it does not exist; 503 if publication is not confirmed |

Order responses contain `order_id`, `request_key`, `product_id`, `quantity`, `unit_price_cents`, `total_cents`, `currency`, `status`, `rejection_reason`, `last_error`, `allocation_id`, `allocation_status`, `payment_id`, `payment_status`, `correlation_id`, `created_at`, `updated_at`, `event_id`, and `event_publication_status`.

The last two fields are null before the event is created.

The replay route returns the outbox record: `event_id`, `order_id`, `event_type`, `payload`, `publication_status`, `created_at`, and `published_at`. The payload contains the original JSON as text.

HTTP 402 Payment Required is the selected business rejection for a declined payment. HTTP 409 Conflict represents unavailable stock or an idempotency conflict.

A 202 Accepted response retains the `order_id` and status so the client can query or resume the same purchase.

Orders configures finite HTTP timeouts with `SERVICE_HTTP_TIMEOUT_SECONDS` and `PAYMENT_HTTP_TIMEOUT_SECONDS` (two seconds by default). A timeout leaves the result unknown until it is queried or resumed; it does not mean the payment was declined.

Internal mutations in Products (`POST /allocations` and its release route) and Payments (`POST /payments`) require `X-Service-Token`.

Products, Payments, and Orders must start with the same local `SERVICE_TOKEN`. Orders adds the header when calling those operations; catalog and payment-result reads do not require the token.

This control is for the local practice, not a complete production security design.

## OrderConfirmed event

Orders creates the event only when payment is approved and the allocation is still active. It saves the confirmed purchase and payload to its outbox in a local SQLite transaction.

The published payload follows this contract.

**Reference example — documentation only; do not run as a command:**

```json
{
  "event_id": "Stable event UUID",
  "event_type": "OrderConfirmed",
  "schema_version": 1,
  "occurred_at": "2026-10-12T15:00:00Z",
  "order_id": "UUID of an existing order",
  "product_id": 1,
  "quantity": 2,
  "total_cents": 100000,
  "currency": "MXN",
  "correlation_id": "Checkout trace UUID"
}
```

The timestamp and UUIDs are generated by Orders. The RabbitMQ routing key is `order.confirmed`; `event_id` is also sent as `message_id`.

Retrying a pending publication or calling the replay route sends the saved payload again with the same `event_id`. It does not create another purchase or a new event.

Notifications validates the contract and saves the confirmation and processed ID in a transaction before acknowledging the message.

Unique constraints in `notifications.db` prevent duplicate confirmations by order or event, even after the worker restarts. Inspect them with:

```powershell
.\.venv\Scripts\python.exe notification_consumer.py --list-confirmations
.\.venv\Scripts\python.exe notification_consumer.py --list-processed-event-ids
```

## Health and dependency availability

`GET /health` on each API (ports 8001, 8002, and 8004) checks that the process responds and that its own SQLite database can be read.

It does not check the availability of other APIs or RabbitMQ.

The endpoint takes no fields and returns HTTP 200 with a response such as:

```json
{
  "status": "ok",
  "service": "products"
}
```

The `service` value is `orders` or `payments` for the other APIs. A local database that cannot be read produces HTTP 500.

To check dependencies separately:

- Query `GET /health` on Products and Payments.
- Run `docker compose ps` and verify that RabbitMQ is healthy.
- Run `setup_broker.py` to confirm that the exchange and queue are declared.
- Check that the Notifications worker shows `operation=start outcome=waiting`.
- After a purchase, query the worker's persisted confirmations using the commands above.

A pending event in Orders means publication needs to be retried, not that the purchase is no longer confirmed.

Notifications does not expose HTTP; it consumes `OrderConfirmed` from RabbitMQ. If the event is valid and saved, the worker acknowledges the message. A repeated delivery is recognized as a duplicate without creating another confirmation.

An invalid or conflicting event is rejected without requeueing. A transient persistence failure is requeued for later processing.

## Reliability patterns in step 6

### Persistent idempotency

Orders enforces `UNIQUE` on `request_key` and retains the order and its payload.

Products uses `BEGIN IMMEDIATE`, one allocation per `order_id`, and `UPDATE ... WHERE stock >= quantity` in the same transaction.

Its SQLite wait is 10 seconds. If the database remains busy, Products responds with HTTP 503 and `Retry-After: 1`, without reporting a successful allocation.

Payments also stores one result per `order_id`. Orders queries the existing result before requesting another.

### Unknown payment and compensation

If Payments does not respond or is busy, Orders stores `recovery_required` and does not release stock for that reason alone.

On resume, Orders first queries the payment using the same `order_id`.

If payment was declined, Orders records `compensation_pending` before releasing the allocation. A release failure keeps that state for retry.

Stock is only increased when the allocation changes from `allocated` to `released`.

### Transactional outbox

`orders.db` saves the confirmed order and the `OrderConfirmed` event in the same transaction.

A unique `order_id` constraint allows one event per purchase, and resume reuses the saved payload.

The `POST /events/publish-pending` endpoint allows retries without a scheduler.

Orders opens a RabbitMQ connection only when publishing. Therefore, it can start, save a confirmed order, and retain the pending event while the broker is offline.

### Safe publication

`broker.py` enables publisher confirms, publishes with `mandatory=True` to detect unroutable messages, and uses persistent messages.

Orders changes the event status to `published` only after broker confirmation.

If the outcome is uncertain, it leaves the record pending. The next attempt uses the same `event_id`, and Notifications deduplicates repeated deliveries.

### Delivery and acknowledgement

`compose.yaml` retains RabbitMQ data in a volume, and the queue is a durable quorum queue.

`setup_broker.py` declares the exchange, queue, and binding before outage tests.

Notifications consumes with `auto_ack=False`, persists the confirmation and processed ID in a transaction, and only then acknowledges the message.

It logs and rejects invalid messages without requeueing. Transient persistence failures are requeued.

## Individual rule: maximum units per order

Orders owns the configurable `MAX_ORDER_QUANTITY` rule because it validates the customer checkout request before coordinating stock or payment.

The default maximum is 10 units. Another positive integer can be configured in `.env`.

A request above the limit is rejected with HTTP 422 during validation, before an order is saved or Products and Payments are called. This rejection does not change stock or payments.

The automated test sends a quantity above the limit and checks that:

- The HTTP 422 response is returned.
- No order is saved.
- Stock does not change.
- No payment or pending event is created.

### Group 11 — Run only the individual-rule test

From the project root, run:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_reliability.CheckoutReliabilityTest.test_quantity_above_limit_is_rejected_without_changing_stock_or_payments
```

## Business rules and states

- `request_key` is unique in Orders. Repeating it with the same product and quantity returns the same order; changing the payload with that key returns HTTP 409.
- Products reserves stock within a transaction and only deducts it when enough units are available. The allocation uses `order_id` as a unique key.
- A confirmed purchase retains its allocation. If Payments declines the payment, Products releases the allocation and marks it released in one transaction. Repeating the release does not add the units again.
- Payments uses `order_id` as the unique key for a result. Before repeating a payment request, Orders queries Payments with that ID. This way, a lost response is not interpreted as a decline and does not create a second payment.
- Order states are `processing`, `recovery_required`, `compensation_pending`, `rejected`, and `confirmed`.
- `recovery_required` retains progress when a dependency fails or a call's outcome is unknown.
- `compensation_pending` means payment was declined and stock release is still incomplete.
- `confirmed` requires approved payment and allocated stock; event publication has its own state.
- Orders saves the confirmed purchase and pending event together. If RabbitMQ is unavailable, the order remains confirmed and the event stays pending for retry.
- Notifications saves the confirmation and processed ID in a transaction before acknowledging the message. A repeated delivery does not create another confirmation.

Stop Python processes with `Ctrl+C` and RabbitMQ with:

```powershell
docker compose stop
```

The SQLite databases remain when services are stopped. Do not delete them if you need to preserve orders, payments, allocations, or confirmations.

## Delivery runbook

Run these commands from the project root. In a newly extracted folder, create the local configuration if it does not exist yet.

### Group 12 — Create `.env`

Run this line once from the project root:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Review `PRODUCTS_URL`, `PAYMENTS_URL`, SQLite paths, timeouts, `SERVICE_TOKEN`, and local RabbitMQ credentials.

The same `SERVICE_TOKEN` must be available to Products, Orders, and Payments.

**Do not include `.env` in the submission or commit real credentials to GitHub.**

### Startup and ports

The full startup configuration is in the sections above.

The order is:

1. RabbitMQ.
2. `setup_broker.py`.
3. Products.
4. Payments.
5. Orders.
6. Notifications.

Run commands from the project root. Open one terminal per API and another for the worker.

| Component | Port |
|---|---:|
| Products | 8001 |
| Orders | 8002 |
| Payments | 8004 |
| RabbitMQ AMQP | 5672 |
| RabbitMQ Management | 15672 |
| Notifications | No HTTP port |

The initial installation requires Python, Docker Desktop with Docker Compose, and possibly internet access for dependencies and images. After that, the demonstration runs locally.

Products creates its catalog when its database is first created. Seeding does not reset stock in an existing database.

### Publish pending events and replay

Retry pending events from any PowerShell terminal while Orders is running.

#### Group 13 — Publish pending events

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8002/events/publish-pending" -Method Post
```

For replay, copy the original `event_id` from `GET /orders/{order_id}`. Replay retains the existing ID and payload.

#### Group 14 — Replay an event

Replace the example ID with the actual event ID and run both lines together:

```powershell
$eventId = "PEGA-AQUI-EL-EVENT-ID"
Invoke-RestMethod -Uri "http://127.0.0.1:8002/events/$eventId/replay" -Method Post
```

For T08, stop Notifications with `Ctrl+C`, create and publish a purchase, and then restart the worker to verify the persisted confirmation.

For T09, stop RabbitMQ with:

```powershell
docker compose stop rabbitmq
```

Complete checkout and record the order as confirmed with a pending event. Restore RabbitMQ with:

```powershell
docker compose up -d rabbitmq
```

Run `setup_broker.py` and call `publish-pending`.

For T10, restart Notifications without deleting its database, replay the event, and check that only one confirmation exists.

## Restart and reset

Restarting with `Ctrl+C` and starting the processes again using the same paths preserves data.

Do not delete databases during T05, T07, T10, or T11.

**The following commands delete local data. Stop the Python processes first.**

From the project root, these commands delete orders, payments, stock/allocations, and confirmations. On startup, new databases will be created and Products will insert the seed data.

### Group 15 — Delete the four SQLite databases

Run these commands together only after stopping all Python processes:

```powershell
Remove-Item -LiteralPath .\products-service\products.db -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath .\orders-service\orders.db -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath .\payments-service\payments.db -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath .\notifications.db -Force -ErrorAction SilentlyContinue
```

**This operation is also destructive:** `docker compose down -v` removes the RabbitMQ Compose volume and its persisted messages and topology. Do not use it during recovery tests.

### Group 16 — Delete RabbitMQ's persisted state

Run this line from the project root only if you want to delete the volume and all its messages:

```powershell
docker compose down -v
```

## Tests and submission documents

The automated tests are component checks with temporary SQLite databases. They do not replace the manual HTTP/RabbitMQ evidence.

Structured evidence for T01–T11 is in `docs/acceptance-evidence.md`; complete it with your screenshots, IDs, and logs.

The individual development record is in `docs/development-record.md` and should be reviewed against your actual history.

The 4–6 page architecture report is submitted as a separate Word document. It may refer to the evidence but does not need to include all T01–T11 details.

### Group 17 — Run all automated tests

From the project root, run:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_reliability
```

### Submission checklist

Before submitting or sharing the project:

- [ ] Review the README and ensure the instructions match the current implementation.
- [ ] Include the required evidence and development-record documents.
- [ ] Run the automated reliability tests.
- [ ] Complete the manual HTTP and RabbitMQ tests with screenshots, IDs, and logs.
- [ ] Exclude `.venv`, `.env`, `*.db` files, `__pycache__`, dependency caches, and large generated files.
- [ ] Verify that `.env` and any real credentials are not committed to GitHub.
- [ ] Preserve local databases and RabbitMQ volumes when completing recovery tests.

`.gitignore` already excludes the local data files described above. Check its contents before publishing to ensure that generated files and secrets remain excluded.
