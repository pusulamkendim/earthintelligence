"""Read live NASA GCN notices without exposing credentials or dumping large skymaps.

Copy .env.example to .env, fill in credentials locally, then run:
    pip install -r requirements-gcn.txt
    python example.py

This is a standalone terminal consumer; it does not ingest into the API yet.
"""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from gcn_kafka import Consumer

TOPICS = [
    "gcn.notices.boom.alert",
    "gcn.circulars",
    "gcn.notices.chime.frb",
    "gcn.notices.dsa110.frb",
    "gcn.notices.einstein_probe.wxt.alert",
    "gcn.heartbeat",
    "gcn.notices.icecube.lvk_nu_track_search",
    "gcn.notices.icecube.gold_bronze_track_alerts",
    "igwn.gwalert",
    "gcn.notices.maxi.gsc.known",
    "gcn.notices.maxi.gsc.unknown",
    "gcn.notices.superk.sn_alert",
    "gcn.notices.swift.bat.guano",
]


def summarize_notice(topic: str, offset: int, raw: bytes) -> dict:
    """Only return small metadata fields; never print FITS/base64 skymaps."""
    try:
        notice = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"topic": topic, "offset": offset, "format": "non-json", "bytes": len(raw)}

    if not isinstance(notice, dict):
        return {
            "topic": topic, "offset": offset, "format": type(notice).__name__,
            "bytes": len(raw),
        }

    event = notice.get("event")
    event = event if isinstance(event, dict) else {}
    event_id = notice.get("superevent_id") or notice.get("event_id") or notice.get("id")
    search = event.get("search")
    mock = (
        isinstance(event_id, str) and event_id.startswith(("MS", "TS"))
    ) or search == "MDC"

    result = {
        "topic": topic,
        "offset": offset,
        "event_id": event_id,
        "alert_type": notice.get("alert_type"),
        "alert_time": notice.get("alert_datetime") or notice.get("time_created"),
        "observed_at": event.get("time") or notice.get("event_time"),
        "is_test": bool(mock),
        "bytes": len(raw),
    }

    if event.get("instruments"):
        result["instruments"] = event["instruments"]
    if isinstance(event.get("classification"), dict):
        result["classification"] = event["classification"]
    if isinstance(event.get("properties"), dict):
        result["properties"] = event["properties"]
    if event.get("skymap") or notice.get("skymap"):
        result["has_skymap"] = True
    for key in ("ra", "dec", "latitude", "longitude"):
        value = notice.get(key)
        if isinstance(value, (int, float)):
            result[key] = value
    return result


def main() -> None:
    load_dotenv()
    client_id = os.getenv("GCN_CLIENT_ID")
    client_secret = os.getenv("GCN_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing GCN_CLIENT_ID or GCN_CLIENT_SECRET. Copy .env.example to .env "
            "and populate it locally; never commit the real .env file."
        )

    consumer = Consumer(client_id=client_id, client_secret=client_secret)
    try:
        consumer.subscribe(TOPICS)
        print(f"Subscribed to {len(TOPICS)} GCN topics. Waiting for alerts (Ctrl+C to stop).", flush=True)
        while True:
            for message in consumer.consume(timeout=1) or []:
                if message.error():
                    print(f"Kafka error: {message.error()}", flush=True)
                    continue
                if message.topic() == "gcn.heartbeat":
                    continue
                summary = summarize_notice(message.topic(), message.offset(), message.value())
                print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
