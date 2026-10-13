"""Deterministic evidence and globe API regression tests."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as api
from app.signals import globe_view, priority, signals_for


NOW = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)


def event(event_id, kind, lat, lon, age_h=1, magnitude=None):
    return {
        "id": event_id, "kind": kind, "source": "usgs" if kind == "earthquake" else "eonet",
        "title": event_id, "lat": lat, "lon": lon,
        "observed_at": (NOW - timedelta(hours=age_h)).isoformat(),
        "magnitude": magnitude, "severity": "medium", "url": None,
        "details": {}, "track": [],
    }


def test_globe_view_contains_ranked_geojson_and_focus():
    quake = event("usgs:test", "earthquake", 36.9, 30.7, magnitude=6.3)
    fire = event("eonet:fire", "wildfire", 37, 30.9)
    view = globe_view([fire, quake], [], now=NOW)
    assert view["counts"] == {"earthquake": 1, "wildfire": 1, "storm": 0}
    assert view["geojson"]["type"] == "FeatureCollection"
    assert view["geojson"]["features"][0]["id"] == "usgs:test"
    assert view["geojson"]["features"][0]["geometry"]["coordinates"] == [30.7, 36.9]
    assert view["signals"][0]["evidence"] == "direct_observation"
    assert view["focus"]["signal_id"] == view["signals"][0]["id"]
    assert view["current_read"]["text"].endswith(".")


def test_old_eonet_events_do_not_create_editorial_signals():
    fire = event("eonet:fire", "wildfire", 37, 30, age_h=900)
    storm = event("eonet:storm", "storm", 37.1, 30.1, age_h=899)
    relations = api.find_relations([fire, storm], radius_km=300, max_hours=48)
    assert len(relations) == 1  # raw proximity query remains available
    assert signals_for([fire, storm], relations, now=NOW) == []


def test_proximity_signal_is_explicitly_noncausal():
    fire = event("eonet:fire", "wildfire", 37, 30, age_h=12)
    storm = event("eonet:storm", "storm", 37.1, 30.1, age_h=11)
    relations = api.find_relations([fire, storm], radius_km=300, max_hours=48)
    found = signals_for([fire, storm], relations, now=NOW)
    assert len(found) == 1
    assert found[0]["evidence"] == "spatiotemporal_proximity_only"
    assert "no impact has been established" in found[0]["current_read"]


def test_lower_priority_for_stale_observations():
    fresh = event("q1", "earthquake", 5, 5, age_h=1, magnitude=5.0)
    old = event("q2", "earthquake", 5, 5, age_h=240, magnitude=5.0)
    assert priority(fresh, now=NOW)["score"] > priority(old, now=NOW)["score"]


def test_http_globe_and_signals(tmp_path, monkeypatch):
    db = api.Store(str(tmp_path / "globe.db"))
    db.put("usgs", [event("usgs:test", "earthquake", 36.9, 30.7, magnitude=6.3)])
    monkeypatch.setattr(api, "store", db)
    client = TestClient(api.app)
    result = client.get("/api/v1/globe")
    assert result.status_code == 200
    body = result.json()
    assert body["returned_features"] == 1
    assert body["signals"][0]["type"] == "significant_earthquake"
    assert client.get("/api/v1/signals").json()["count"] == 1
    read = client.get("/api/v1/current-read").json()
    assert read["counts"]["earthquake"] == 1
    assert read["method"] == "deterministic_ranked_signals"
