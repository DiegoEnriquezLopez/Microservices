import json
import os
import pika
from dotenv import load_dotenv

load_dotenv()

EXCHANGE = "store.events"
QUEUE = os.getenv("STORE_QUEUE", "notifications.confirmed.practice3")
ROUTING_KEY = "order.confirmed"


def connect():
    credentials = pika.PlainCredentials(
        os.getenv("RABBITMQ_USER", "student"),
        os.getenv("RABBITMQ_PASSWORD", "student-pass"),
    )
    parameters = pika.ConnectionParameters(
        host=os.getenv("RABBITMQ_HOST", "127.0.0.1"),
        port=int(os.getenv("RABBITMQ_PORT", "5672")),
        credentials=credentials,
        heartbeat=30,
        blocked_connection_timeout=5,
        socket_timeout=3,
        stack_timeout=5,
        connection_attempts=1,
    )
    return pika.BlockingConnection(parameters)


def declare_topology(channel):
    channel.exchange_declare(
        exchange=EXCHANGE, exchange_type="direct", durable=True
    )
    channel.queue_declare(
        queue=QUEUE,
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments={"x-queue-type": "quorum"},
    )
    channel.queue_bind(
        exchange=EXCHANGE, queue=QUEUE, routing_key=ROUTING_KEY
    )


def publish_event(event):
    connection = connect()
    try:
        channel = connection.channel()
        declare_topology(channel)
        channel.confirm_delivery()
        confirmed = channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=ROUTING_KEY,
            body=json.dumps(event).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=event["event_id"],
            ),
            mandatory=True,
        )
        if confirmed is False:
            raise RuntimeError("RabbitMQ negatively acknowledged the event")
    finally:
        if connection.is_open:
            connection.close()
