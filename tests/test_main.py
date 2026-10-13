from datetime import datetime, timedelta, timezone

from app.main import Store, distance_km, find_relations, make_read, normalize_eonet, normalize_usgs


def test_usgs_normalization():
    data = {"features": [{"id": "abc", "geometry": {"type": "Point", "coordinates": [28.1, 36.9, 12]},
                          "properties": {"time": 1780000000000, "mag": 6.2, "title": "Test quake", "url": "https://example.com"}}]}
    e = normalize_usgs(data)[0]
    assert (e["lat"], e["lon"], e["severity"], e["details"]["depth_km"]) == (36.9, 28.1, "high", 12)


def test_eonet_last_point_and_track():
    data = {"events": [{"id": "E1", "title": "Storm A", "categories": [{"id": "severeStorms"}], "closed": None,
                        "geometry": [{"date": "2026-10-10T00:00:00Z", "type": "Point", "coordinates": [25, 20]},
                                     {"date": "2026-10-11T00:00:00Z", "type": "Point", "coordinates": [28, 21]}]}]}
    e = normalize_eonet(data)[0]
    assert e["kind"] == "storm" and e["lon"] == 28 and len(e["track"]) == 2


def test_relations_are_proximity_not_causality():
    at = datetime.now(timezone.utc).isoformat()
    fire = {"id": "1", "kind": "wildfire", "lat": 37, "lon": 30, "observed_at": at}
    storm = {"id": "2", "kind": "storm", "lat": 37.2, "lon": 30.1, "observed_at": at}
    matches = find_relations([fire, storm])
    assert len(matches) == 1 and matches[0]["confidence"] == "proximity_only"
    assert distance_km(fire, storm) < 30
    storm["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    assert find_relations([fire, storm]) == []


def test_snapshot_as_of(tmp_path):
    db = Store(str(tmp_path / "data.db"))
    event = {"id": "a", "kind": "earthquake", "observed_at": "2026-10-13T00:00:00+00:00", "magnitude": 5.1}
    fetched = db.put("usgs", [event])
    assert len(db.read(datetime.fromisoformat(fetched))) == 1
    assert db.read(datetime.fromisoformat(fetched) - timedelta(seconds=1)) == []
    read = make_read([event], [])
    assert "M5.1" in read["text"]
