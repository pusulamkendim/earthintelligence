"""Earth Intelligence: small, source-attributed geospatial event API."""
from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from app.signals import globe_view, signals_for

UTC = timezone.utc
USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH_SECONDS = max(60, int(os.getenv("EARTH_REFRESH_SECONDS", "300")))
EONET_REFRESH_SECONDS = max(300, int(os.getenv("EONET_REFRESH_SECONDS", "1800")))
EONET_LIMIT = min(5000, max(300, int(os.getenv("EONET_LIMIT", "2000"))))
DB_PATH = os.getenv("EARTH_DB_PATH", "data/earth.db")
REFRESH_TOKEN = os.getenv("EARTH_REFRESH_TOKEN", "")
SOURCES = {
    "usgs": {"name": "USGS Earthquake Hazards Program", "url": USGS_URL},
    "eonet": {"name": "NASA EONET", "url": EONET_URL},
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def parse_date(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    raise ValueError("invalid observation date")


def valid_point(lon: Any, lat: Any) -> tuple[float, float] | None:
    try:
        x, y = float(lon), float(lat)
        if math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90:
            return x, y
    except (TypeError, ValueError):
        pass
    return None


def representative_point(geometry: dict[str, Any]) -> tuple[float, float] | None:
    typ, coords = geometry.get("type"), geometry.get("coordinates")
    if typ == "Point" and isinstance(coords, list) and len(coords) >= 2:
        return valid_point(coords[0], coords[1])
    if typ == "Polygon" and isinstance(coords, list) and coords and coords[0]:
        pts = [valid_point(p[0], p[1]) for p in coords[0] if len(p) >= 2]
        pts = [p for p in pts if p]
        if pts:
            # Representative for marker placement, not an exact polygon centroid.
            return pts[0]
    return None


def normalize_usgs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for feature in payload.get("features", []):
        props, geo = feature.get("properties") or {}, feature.get("geometry") or {}
        point = representative_point(geo)
        if not point or not feature.get("id") or props.get("time") is None:
            continue
        try:
            measured = parse_date(props["time"]).isoformat()
        except (ValueError, OverflowError):
            continue
        mag = props.get("mag")
        magnitude = float(mag) if isinstance(mag, (int, float)) else None
        result.append({
            "id": "usgs:" + str(feature["id"]), "source": "usgs", "kind": "earthquake",
            "title": props.get("title") or "Earthquake", "lon": point[0], "lat": point[1],
            "observed_at": measured, "severity": "high" if magnitude is not None and magnitude >= 6 else "medium" if magnitude is not None and magnitude >= 5 else "low",
            "magnitude": magnitude, "url": props.get("url"),
            "details": {"depth_km": (geo.get("coordinates") or [None, None, None])[2] if len(geo.get("coordinates") or []) > 2 else None, "tsunami_flag": props.get("tsunami")},
            "track": [],
        })
    return result


def normalize_eonet(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for event in payload.get("events", []):
        cats = [c.get("id") for c in event.get("categories", [])]
        kind = "wildfire" if "wildfires" in cats else "storm" if "severeStorms" in cats else None
        if not kind or not event.get("id"):
            continue
        track = []
        for item in event.get("geometry", []):
            point = representative_point(item)
            if not point:
                continue
            try:
                date = parse_date(item["date"]).isoformat()
            except (KeyError, ValueError, OverflowError):
                continue
            track.append({"at": date, "lon": point[0], "lat": point[1]})
        if not track:
            continue
        track.sort(key=lambda p: p["at"])
        latest = track[-1]
        result.append({
            "id": "eonet:" + str(event["id"]), "source": "eonet", "kind": kind,
            "title": event.get("title") or ("Wildfire" if kind == "wildfire" else "Storm"),
            "lon": latest["lon"], "lat": latest["lat"], "observed_at": latest["at"],
            "severity": "medium", "magnitude": event.get("magnitudeValue"),
            "url": event.get("link"), "details": {"active": event.get("closed") is None, "categories": cats, "magnitude_unit": event.get("magnitudeUnit")},
            "track": track[-100:],
        })
    return result


def distance_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(b["lon"] - a["lon"])
    h = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def find_relations(events: list[dict[str, Any]], radius_km: float = 600, max_hours: int = 72) -> list[dict[str, Any]]:
    """Spatiotemporal co-occurrence only: never implies cause/effect."""
    fires = [e for e in events if e["kind"] == "wildfire"]
    storms = [e for e in events if e["kind"] == "storm"]
    found = []
    for fire in fires:
        for storm in storms:
            delta = abs((parse_date(fire["observed_at"]) - parse_date(storm["observed_at"])).total_seconds()) / 3600
            if delta > max_hours:
                continue
            km = distance_km(fire, storm)
            if km <= radius_km:
                found.append({"type": "spatiotemporal_proximity", "event_ids": [fire["id"], storm["id"]],
                              "distance_km": round(km, 1), "time_gap_hours": round(delta, 1),
                              "interpretation": "Wildfire and storm reports are nearby in space and time; causation is not established.",
                              "confidence": "proximity_only"})
    return sorted(found, key=lambda r: (r["distance_km"], r["time_gap_hours"]))


def make_read(events: list[dict[str, Any]], relations: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {kind: sum(e["kind"] == kind for e in events) for kind in ("earthquake", "wildfire", "storm")}
    strongest = max((e for e in events if e["kind"] == "earthquake" and e["magnitude"] is not None), key=lambda e: e["magnitude"], default=None)
    if not events:
        text = "No observations loaded yet. Check source status."
    else:
        text = f"Latest feed snapshots contain {counts['earthquake']} earthquakes, {counts['wildfire']} open wildfire events and {counts['storm']} open storm events."
        if strongest:
            text += f" Strongest listed earthquake: M{strongest['magnitude']:.1f}."
        if relations:
            text += f" {len(relations)} wildfire/storm proximity matches need review; no causal link is claimed."
    return {"text": text, "counts": counts, "relation_count": len(relations), "generated_at": now_iso(),
            "method": "deterministic_source_summary", "note": "Feeds have different observation times and spatial coverage."}


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS batches (source TEXT NOT NULL, fetched_at TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(source, fetched_at))")
            db.execute("CREATE TABLE IF NOT EXISTS observations (source TEXT NOT NULL, fetched_at TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(source, fetched_at, event_id))")
            db.execute("CREATE INDEX IF NOT EXISTS obs_batch_idx ON observations(source, fetched_at)")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def put(self, source: str, events: list[dict[str, Any]]) -> str:
        fetched = now_iso()
        with self.connect() as db:
            db.execute("INSERT INTO batches VALUES (?,?,?)", (source, fetched, len(events)))
            db.executemany("INSERT INTO observations VALUES (?,?,?,?)", [(source, fetched, e["id"], json.dumps(e)) for e in events])
            # Retain up to 7 days of observed snapshots for local replay.
            cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
            db.execute("DELETE FROM observations WHERE fetched_at < ?", (cutoff,))
            db.execute("DELETE FROM batches WHERE fetched_at < ?", (cutoff,))
        return fetched

    def read(self, as_of: datetime | None = None) -> list[dict[str, Any]]:
        cutoff = as_of.astimezone(UTC).isoformat() if as_of else now_iso()
        query = """SELECT o.payload FROM observations o JOIN
           (SELECT source, MAX(fetched_at) AS newest FROM batches WHERE fetched_at <= ? GROUP BY source) b
           ON o.source=b.source AND o.fetched_at=b.newest"""
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, (cutoff,))]

    def source_times(self) -> dict[str, dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""SELECT b.source,b.fetched_at,b.count FROM batches b JOIN
              (SELECT source, MAX(fetched_at) AS latest FROM batches GROUP BY source) x
              ON b.source=x.source AND b.fetched_at=x.latest""").fetchall()
        return {name: {"last_success": fetched, "count": count} for name, fetched, count in rows}


store: Store | None = None
client: httpx.AsyncClient | None = None
errors: dict[str, str | None] = {"usgs": None, "eonet": None}
coverage: dict[str, dict[str, Any]] = {
    "usgs": {"feed": "M2.5+ earthquakes in last 24 hours"},
    "eonet": {"status": "not_loaded", "requested_limit": EONET_LIMIT},
}
refresh_lock = asyncio.Lock()


async def refresh(force: bool = False) -> dict[str, Any]:
    if client is None or store is None:
        raise RuntimeError("Application not initialized")
    async with refresh_lock:
        async def fetch(source: str, url: str, params: dict[str, Any] | None, normalizer: Any):
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                if source == "eonet":
                    raw_count = len(payload.get("events", []))
                    coverage["eonet"] = {
                        "status": "possibly_limited" if raw_count >= EONET_LIMIT else "within_requested_limit",
                        "requested_limit": EONET_LIMIT, "returned_raw": raw_count,
                        "possibly_truncated": raw_count >= EONET_LIMIT,
                        "note": "EONET open status and source dates do not guarantee current physical activity.",
                    }
                events = normalizer(payload)
                fetched = store.put(source, events)
                errors[source] = None
                return {"source": source, "count": len(events), "fetched_at": fetched, "status": "ok"}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                errors[source] = str(exc)[:250]
                return {"source": source, "status": "error", "error": errors[source]}

        last = store.source_times().get("eonet", {}).get("last_success")
        eonet_due = force or not last or (
            datetime.now(UTC) - parse_date(last)
        ).total_seconds() >= EONET_REFRESH_SECONDS
        tasks = [fetch("usgs", USGS_URL, None, normalize_usgs)]
        if eonet_due:
            tasks.append(fetch("eonet", EONET_URL, {
                "status": "open", "category": "wildfires,severeStorms",
                "limit": EONET_LIMIT}, normalize_eonet))
        results = await asyncio.gather(*tasks)
        if not eonet_due:
            results.append({"source": "eonet", "status": "skipped",
                            "reason": "refresh_interval", "interval_seconds": EONET_REFRESH_SECONDS})
        return {"sources": results}


async def polling() -> None:
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        await refresh()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global client, store
    store = Store(DB_PATH)
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "EarthIntelligence-MVP/0.1 (+public-data)"}) as http:
        client = http
        await refresh()
        task = asyncio.create_task(polling())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            client = None


app = FastAPI(title="Earth Intelligence API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in os.getenv("EARTH_CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",") if x.strip()], allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Refresh-Token"])


def active_store() -> Store:
    if store is None:
        raise HTTPException(503, "Application not initialized")
    return store


@app.get("/api/v1/health")
def health():
    batches = active_store().source_times()
    return {"status": "ok" if len(batches) == len(SOURCES) and not any(errors.values()) else "degraded", "sources_ready": len(batches), "source_errors": errors, "time": now_iso()}


@app.get("/api/v1/sources")
def sources():
    times = active_store().source_times()
    return {name: {**meta, **times.get(name, {}), "last_error": errors.get(name),
                   "coverage": coverage.get(name, {})} for name, meta in SOURCES.items()}


def selected_events(as_of: datetime | None, kind: str | None, limit: int) -> list[dict[str, Any]]:
    rows = active_store().read(as_of)
    if kind:
        rows = [e for e in rows if e["kind"] == kind]
    return sorted(rows, key=lambda e: e["observed_at"], reverse=True)[:limit]


@app.get("/api/v1/events")
def events(kind: str | None = Query(None, pattern="^(earthquake|wildfire|storm)$"),
           limit: int = Query(200, ge=1, le=1500), as_of: datetime | None = None):
    rows = selected_events(as_of, kind, limit)
    return {"count": len(rows), "as_of": as_of, "events": rows}


@app.get("/api/v1/events.geojson")
def geojson(kind: str | None = Query(None, pattern="^(earthquake|wildfire|storm)$"),
            limit: int = Query(500, ge=1, le=1500), as_of: datetime | None = None):
    rows = selected_events(as_of, kind, limit)
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "id": e["id"],
            "geometry": {"type": "Point", "coordinates": [e["lon"], e["lat"]]},
            "properties": {k: v for k, v in e.items() if k not in ("lon", "lat")}} for e in rows]}


@app.get("/api/v1/relations")
def relations(radius_km: float = Query(600, ge=1, le=2000), max_hours: int = Query(72, ge=1, le=720), as_of: datetime | None = None):
    matches = find_relations(active_store().read(as_of), radius_km, max_hours)
    return {"count": len(matches), "relations": matches[:100], "disclaimer": "Spatiotemporal co-occurrence is not evidence of causation."}


@app.get("/api/v1/current-read")
def current_read(as_of: datetime | None = None):
    all_events = active_store().read(as_of)
    relations = find_relations(all_events, radius_km=300, max_hours=48)
    globe_data = globe_view(all_events, relations, limit=1, now=as_of)
    return {
        **globe_data["current_read"],
        "counts": globe_data["counts"],
        "relation_count": len(relations),
        "generated_at": globe_data["generated_at"],
        "source_coverage": coverage,
        "note": "Editorial signals are not scientific proof of causal impact.",
    }


@app.get("/api/v1/signals")
def signals(limit: int = Query(30, ge=1, le=100), as_of: datetime | None = None):
    all_events = active_store().read(as_of)
    matches = find_relations(all_events, radius_km=300, max_hours=48)
    result = signals_for(all_events, matches, now=as_of)
    return {"count": len(result), "signals": result[:limit],
            "evidence_policy": "No causality inferred from proximity alone."}


@app.get("/api/v1/globe")
def globe(limit: int = Query(2000, ge=1, le=5000), as_of: datetime | None = None):
    all_events = active_store().read(as_of)
    matches = find_relations(all_events, radius_km=300, max_hours=48)
    view = globe_view(all_events, matches, limit=limit, now=as_of)
    view["sources"] = {
        name: {**details, **active_store().source_times().get(name, {}),
               "coverage": coverage.get(name, {})}
        for name, details in SOURCES.items()
    }
    view["as_of"] = as_of
    return view


@app.get("/api/v1/timeline/{event_id:path}")
def timeline(event_id: str):
    matches = [e for e in active_store().read() if e["id"] == event_id]
    if not matches:
        raise HTTPException(404, "Event not in latest feed snapshots")
    e = matches[0]
    return {"id": event_id, "track": e["track"], "observed_at": e["observed_at"],
            "note": "EONET track is source geometry history, not continuously recorded telemetry."}


@app.get("/api/v1/weather")
async def weather(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    if client is None:
        raise HTTPException(503, "Application not initialized")
    try:
        resp = await client.get(WEATHER_URL, params={"latitude": lat, "longitude": lon,
            "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,precipitation,cloud_cover", "timezone": "UTC"})
        resp.raise_for_status()
        values = resp.json().get("current") or {}
        return {"source": "open-meteo", "type": "model_estimate", "latitude": lat, "longitude": lon,
                "values": values, "units": resp.json().get("current_units", {}),
                "note": "Model-based current weather is not a direct local measurement."}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Weather provider unavailable: {str(exc)[:180]}") from exc


@app.post("/api/v1/refresh")
async def refresh_now(x_refresh_token: str | None = Header(None)):
    if not REFRESH_TOKEN or x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(403, "Set EARTH_REFRESH_TOKEN and supply X-Refresh-Token")
    return await refresh(force=True)
