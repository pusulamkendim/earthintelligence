# Earth Intelligence - Backend MVP

Keyless public-data API for a future 3D globe. It normalizes **USGS earthquakes (M2.5+, last day)** and **NASA EONET open wildfire/storm events** into one coordinate schema. An optional **Open-Meteo** endpoint provides model-based local wind/precipitation estimates.

## Run (macOS / Linux)

~~~bash
git clone https://github.com/pusulamkendim/earthintelligence.git
cd earthintelligence
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
~~~

On launch the server fetches both public sources, then refreshes automatically every 5 minutes. A source failure does not stop the server; inspect /api/v1/health and /api/v1/sources.

## Test live data (another terminal)

~~~bash
curl -sS http://127.0.0.1:8000/api/v1/health | python3 -m json.tool
curl -sS http://127.0.0.1:8000/api/v1/sources | python3 -m json.tool
curl -sS 'http://127.0.0.1:8000/api/v1/events?kind=earthquake&limit=3' | python3 -m json.tool
curl -sS 'http://127.0.0.1:8000/api/v1/events.geojson?limit=500' > globe.geojson
curl -sS http://127.0.0.1:8000/api/v1/current-read | python3 -m json.tool
curl -sS 'http://127.0.0.1:8000/api/v1/relations?radius_km=1000&max_hours=168' | python3 -m json.tool
curl -sS 'http://127.0.0.1:8000/api/v1/weather?lat=36.9&lon=30.7' | python3 -m json.tool
~~~

API documentation: http://127.0.0.1:8000/docs

### Replay and history

Successful fetches are saved in SQLite for seven days. Pass ?as_of=2026-10-13T18:00:00Z to events, events.geojson, relations, or current-read to replay **snapshots captured by this server after it began running**. NASA EONET event tracks are also provided at /api/v1/timeline/eonet:EVENT_ID. No earlier snapshot history is fabricated.

### Configuration

- EARTH_DB_PATH (default data/earth.db)
- EARTH_REFRESH_SECONDS (default 300; min 60)
- EARTH_CORS_ORIGINS (comma-separated allowed frontends)
- EARTH_REFRESH_TOKEN (optional; if set, enables POST /api/v1/refresh with X-Refresh-Token header)

Docker: docker build -t earth-intelligence . && docker run --rm -p 8000:8000 -v earth-data:/service/data earth-intelligence

## Data semantics and limitations

- EONET open events are **not** raw fire hotspots or live storm telemetry. Their locations/dates can be sparse or delayed; event geometry history is not equivalent to server-captured snapshot replay.
- Open-Meteo current values are **model estimates**, not direct instruments or measured causal effects.
- Relations are spatial/temporal proximity of *wildfire* and *storm* records only. They do **not** prove either caused or affected the other.
- Current Read is deterministic text using feed counts and nearby-event matches. No LLM and no speculative causal statements.
- The initial feed is global but coverage, cadence, licensing and attribution differ by upstream. Re-check commercial redistribution terms before product launch.
- If you deploy publicly, restrict refresh token, rate-limit endpoints and monitor database volume. SQLite is intended for prototyping, not horizontal scaling.

Attribution: [USGS](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php), [NASA EONET](https://eonet.gsfc.nasa.gov/docs/v3), [Open-Meteo](https://open-meteo.com/en/docs).
