"""Evidence-aware, source-attributed signals and globe view models.

This module intentionally avoids claiming causality. NASA EONET event coordinates
may be coarse and their geometry timestamps may be midnight placeholders.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def age_hours(when: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    observed = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - observed).total_seconds() / 3600)


def priority(event: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Editorial sorting, not a scientific hazard-risk score."""
    kind = event["kind"]
    magnitude = event.get("magnitude")
    hours = age_hours(event["observed_at"], now)
    if kind == "earthquake":
        score = 50 + max(0, float(magnitude or 0) - 4) * 17
    elif kind == "storm":
        score = 48
    else:
        score = 31
    freshness = max(0.05, math.exp(-hours / (24 if kind == "earthquake" else 168)))
    score = round(min(100, score * freshness), 1)
    return {
        "score": score,
        "freshness_hours": round(hours, 1),
        "freshness": "recent" if hours <= 24 else "aging" if hours <= 168 else "old",
        "reason": "Magnitude and observation age" if kind == "earthquake" else "Event category and observation age",
        "scientific_hazard_score": False,
    }


def signals_for(events: list[dict[str, Any]], relations: list[dict[str, Any]],
                now: datetime | None = None) -> list[dict[str, Any]]:
    """Rank noteworthy observations; proximity never becomes an asserted effect."""
    now = now or datetime.now(timezone.utc)
    signals: list[dict[str, Any]] = []
    by_id = {event["id"]: event for event in events}
    for event in events:
        if event["kind"] != "earthquake" or event.get("magnitude") is None:
            continue
        mag = float(event["magnitude"])
        age = age_hours(event["observed_at"], now)
        if mag < 5 or age > 48:
            continue
        signals.append({
            "id": "quake:" + event["id"], "type": "significant_earthquake",
            "level": "high" if mag >= 6 else "medium", "evidence": "direct_observation",
            "score": round(min(100, 65 + (mag - 5) * 14) * math.exp(-age / 96), 1),
            "title": f"M{mag:.1f} earthquake reported",
            "current_read": f"A magnitude {mag:.1f} earthquake was reported: {event['title']}.",
            "event_ids": [event["id"]], "lat": event["lat"], "lon": event["lon"],
            "observed_at": event["observed_at"], "source": "usgs",
            "caveat": "Magnitude and coordinates may be revised by the source.",
        })
    for relation in relations:
        if relation.get("type") != "spatiotemporal_proximity":
            continue
        ids = relation.get("event_ids", [])
        if len(ids) != 2 or any(i not in by_id for i in ids):
            continue
        a, b = by_id[ids[0]], by_id[ids[1]]
        if {a["kind"], b["kind"]} != {"wildfire", "storm"}:
            continue
        km = float(relation["distance_km"])
        gap = float(relation["time_gap_hours"])
        # Conservative editorial gate; still not a causal association.
        if km > 300 or gap > 48:
            continue
        if min(age_hours(a["observed_at"], now), age_hours(b["observed_at"], now)) > 72:
            continue
        signals.append({
            "id": "proximity:" + ":".join(sorted(ids)), "type": "storm_fire_proximity",
            "level": "watch", "evidence": "spatiotemporal_proximity_only",
            "score": round(45 * (1 - km / 600) * (1 - gap / 96), 1),
            "title": "Storm and wildfire records are nearby",
            "current_read": f"Storm and wildfire reports are about {km:.0f} km apart, with an observation-time gap of {gap:.0f} hours; no impact has been established.",
            "event_ids": ids, "lat": (a["lat"] + b["lat"]) / 2,
            "lon": (a["lon"] + b["lon"]) / 2,
            "observed_at": max(a["observed_at"], b["observed_at"]),
            "source": "nasa-eonet",
            "caveat": "EONET location and date precision varies. Proximity is not evidence of causality.",
        })
    return sorted(signals, key=lambda s: (-s["score"], s["id"]))


def globe_view(events: list[dict[str, Any]], relations: list[dict[str, Any]],
               limit: int = 2000, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    scored = [(event, priority(event, now)) for event in events]
    scored.sort(key=lambda pair: (-pair[1]["score"], pair[0]["id"]))
    features = [{
        "type": "Feature", "id": e["id"],
        "geometry": {"type": "Point", "coordinates": [e["lon"], e["lat"]]},
        "properties": {
            "id": e["id"], "kind": e["kind"], "title": e["title"],
            "source": e["source"], "observed_at": e["observed_at"],
            "severity": e.get("severity"), "magnitude": e.get("magnitude"),
            "url": e.get("url"), "priority": p,
        },
    } for e, p in scored[:limit]]
    signals = signals_for(events, relations, now)
    counts = {kind: sum(e["kind"] == kind for e in events) for kind in ("earthquake", "wildfire", "storm")}
    if signals:
        read = signals[0]["current_read"]
    elif events:
        read = f"Latest source snapshots contain {counts['earthquake']} earthquakes, {counts['wildfire']} wildfire records and {counts['storm']} storm records; no high-priority signal meets the current rules."
    else:
        read = "No event observations are available yet; check source status."
    return {
        "generated_at": now.isoformat(), "current_read": {
            "text": read, "method": "deterministic_ranked_signals",
            "evidence": signals[0]["evidence"] if signals else "feed_counts",
            "signal_id": signals[0]["id"] if signals else None,
        },
        "signals": signals[:30],
        "focus": {"lat": signals[0]["lat"], "lon": signals[0]["lon"],
                  "signal_id": signals[0]["id"]} if signals else None,
        "counts": counts, "total_observations": len(events),
        "returned_features": len(features),
        "geojson": {"type": "FeatureCollection", "features": features},
        "disclaimer": "Priority is editorial. Proximity is not proof of influence or causation. Data coverage differs by source.",
    }
