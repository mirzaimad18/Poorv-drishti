"""Publish made-up inventory readings for local MQTT integration testing."""

import argparse
import json
import os
import random
import time
from datetime import datetime, timezone


def topic_for_post(post_id: int) -> str:
    if post_id <= 0:
        raise ValueError("post_id must be positive")
    return f"purva/stock/{post_id}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10, help="number of synthetic updates (0 runs continuously)")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between updates")
    parser.add_argument("--post-id", type=int, default=1)
    parser.add_argument("--item-id", type=int, default=1)
    args = parser.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except (ImportError, ModuleNotFoundError) as exc:
        raise SystemExit(f"Install paho-mqtt to publish: {exc}")

    client = mqtt.Client()
    client.connect(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")), 60)
    client.loop_start()
    topic = topic_for_post(args.post_id)
    published = 0
    try:
        while args.count == 0 or published < args.count:
            payload = {
                "post_id": args.post_id,
                "item_id": args.item_id,
                "quantity": round(random.uniform(40, 280), 2),
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "synthetic": True,
            }
            result = client.publish(topic, json.dumps(payload), qos=1)
            result.wait_for_publish()
            print(json.dumps(payload))
            published += 1
            if args.count == 0 or published < args.count:
                time.sleep(max(0, args.interval))
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
