"""Offline tests for the semiconductor-fab source (pure logic, no network)."""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "src" / "save_earth" / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from _save_earth_tools import semiconductor as S  # noqa: E402


def test_to_feature_keeps_all_tags_for_popup():
    """Every OSM tag must survive into properties (the popup shows them all),
    plus derived provenance fields; ways use their Overpass centroid."""
    el = {
        "type": "way",
        "id": 42,
        "center": {"lat": 24.77, "lon": 120.99},
        "tags": {"name": "Fab 12", "operator": "TSMC", "industrial": "semiconductor",
                 "product": "integrated_circuit"},
    }
    feat = S._to_feature(el)
    assert feat["geometry"] == {"type": "Point", "coordinates": [120.99, 24.77]}
    props = feat["properties"]
    for k in ("name", "operator", "industrial", "product"):
        assert k in props  # all OSM tags preserved
    assert props["osm_type"] == "way" and props["osm_id"] == 42
    assert props["osm_url"] == "https://www.openstreetmap.org/way/42"


def test_to_feature_skips_untagged_or_geometryless():
    assert S._to_feature({"type": "node", "id": 1, "lat": 1.0, "lon": 2.0, "tags": {}}) is None
    assert S._to_feature({"type": "way", "id": 2, "tags": {"name": "x"}}) is None  # no center


def test_query_is_per_country_and_covers_the_tag_schemes():
    q = S._QUERY_TMPL.format(t=120, iso2="TW")
    assert 'area["ISO3166-1"="TW"]' in q  # per-country area, the fan-out leaf
    assert 'nwr["industrial"="semiconductor"]' in q
    assert "out center tags;" in q  # centroids + all tags


def test_cache_is_first_no_auto_refresh():
    """A present cache must never expire on its own — only force re-queries."""
    assert S.DEFAULT_MAX_AGE_HOURS == float("inf")


def test_persist_and_merge_round_trip(tmp_path, monkeypatch):
    """Writing per-country caches and merging them must round-trip (regression:
    merge_fabs once called a non-existent sidecar.write_artifact)."""
    import json

    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    from _save_earth_tools.storage import get_storage

    st = get_storage()
    for iso, lat, lon in (("TW", 24.0, 121.0), ("KR", 37.0, 127.0)):
        feat = S._to_feature(
            {"type": "node", "id": int(lat), "lat": lat, "lon": lon,
             "tags": {"name": iso, "industrial": "semiconductor"}}
        )
        body = json.dumps({"type": "FeatureCollection", "features": [feat]}).encode()
        S._persist(f"by-country/{iso}.geojson", body, st, source_url="x",
                   extra={"feature_count": 1})
    res = S.merge_fabs(["by-country/TW.geojson", "by-country/KR.geojson"], storage=st)
    assert res.feature_count == 2 and res.country_count == 2
    assert res.relative_path == "fabs.geojson"
