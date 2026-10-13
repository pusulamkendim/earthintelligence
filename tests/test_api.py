from datetime import datetime, timezone
from fastapi.testclient import TestClient
import app.main as api


def test_http_endpoints_return_globe_ready_geojson(tmp_path, monkeypatch):
    store = api.Store(str(tmp_path / 'earth.db'))
    t = datetime.now(timezone.utc).isoformat()
    store.put('usgs', [{
        'id': 'usgs:demo', 'kind': 'earthquake', 'source': 'usgs', 'title': 'Demo',
        'lat': 36.9, 'lon': 30.7, 'observed_at': t, 'magnitude': 5.2,
        'track': [], 'details': {}, 'severity': 'medium', 'url': None,
    }])
    monkeypatch.setattr(api, 'store', store)
    client = TestClient(api.app)
    response = client.get('/api/v1/events.geojson?kind=earthquake')
    assert response.status_code == 200
    assert response.json()['features'][0]['geometry']['coordinates'] == [30.7, 36.9]
    assert client.get('/api/v1/current-read').json()['counts']['earthquake'] == 1
    assert client.get('/api/v1/relations').json()['count'] == 0
    assert client.get('/api/v1/health').status_code == 200
    assert client.get('/api/v1/events?kind=unknown').status_code == 422
    assert client.post('/api/v1/refresh').status_code == 403
