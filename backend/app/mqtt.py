"""Optional MQTT subscriber that applies synthetic stock updates to PostGIS."""

import json
import logging
import os
import threading

from sqlalchemy import select

from .db import SessionLocal
from .models import StockLevel

logger = logging.getLogger(__name__)
_client = None
_lock = threading.Lock()
status = {"available": False, "connected": False, "error": None}


def _post_id_from_topic(topic: str) -> int:
    parts = topic.split("/")
    if len(parts) != 3 or parts[:2] != ["purva", "stock"]:
        raise ValueError("Topic must be purva/stock/{post_id}")
    post_id = int(parts[2])
    if post_id <= 0:
        raise ValueError("Topic post_id must be positive")
    return post_id


def _on_connect(client, userdata, flags, rc, properties=None):
    if rc:
        status.update(connected=False, error=f"MQTT connection refused (code {rc})")
        logger.error(status["error"])
        return
    status.update(connected=True, error=None)
    client.subscribe("purva/stock/+")


def _on_disconnect(client, userdata, disconnect_flags=None, rc=None, properties=None):
    reason_code = rc if rc is not None else disconnect_flags
    status.update(connected=False)
    if reason_code:
        status.update(error=f"MQTT disconnected (code {reason_code})")
        logger.error(status["error"])


def _on_message(client, userdata, message):
    try:
        payload = json.loads(message.payload.decode("utf-8"))
        item_id = int(payload["item_id"])
        quantity = float(payload["quantity"])
        post_id = _post_id_from_topic(message.topic)
        if payload.get("post_id") is not None and int(payload["post_id"]) != post_id:
            raise ValueError("Payload post_id must match the topic post_id")
        if quantity < 0:
            raise ValueError("Quantity must be non-negative")
        with SessionLocal() as session:
            query = select(StockLevel).where(
                StockLevel.item_id == item_id,
                StockLevel.depot_id.is_(None),
                StockLevel.post_id == post_id,
            )
            stock = session.scalar(query)
            if stock is None:
                stock = StockLevel(
                    item_id=item_id,
                    depot_id=None,
                    post_id=post_id,
                    quantity=quantity,
                )
                session.add(stock)
            else:
                stock.quantity = quantity
            session.commit()
        status.update(error=None)
    except Exception as exc:
        status.update(error=f"MQTT update rejected: {type(exc).__name__}: {exc}")
        logger.exception(status["error"])


def start_subscriber() -> None:
    """Start one background Paho client; MQTT is reported as unavailable if absent."""
    global _client
    with _lock:
        if _client is not None:
            return
        try:
            import paho.mqtt.client as mqtt
        except (ImportError, ModuleNotFoundError) as exc:
            status.update(available=False, connected=False, error=f"paho-mqtt unavailable: {exc}")
            logger.warning(status["error"])
            return

        status.update(available=True, connected=False, error=None)
        client = mqtt.Client()
        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_message = _on_message
        host = os.getenv("MQTT_HOST", "localhost")
        port = int(os.getenv("MQTT_PORT", "1883"))
        try:
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
            _client = client
        except Exception as exc:
            status.update(error=f"MQTT connection failed: {type(exc).__name__}: {exc}")
            logger.exception(status["error"])


def stop_subscriber() -> None:
    global _client
    with _lock:
        if _client is not None:
            _client.loop_stop()
            _client.disconnect()
            _client = None
        status.update(connected=False)
