"""Offline tests for the ALPR (DeFlock) surveillance-camera source adapter.

Fully offline via ``use_mock=True`` (no Overpass, no network): exercises the
download → cache → sidecar path, the GeoJSON shape, the verbatim-tags +
derived-field contract (osm_url, camera_vendor), and the cache short-circuit.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


def _load(res):
    from save_earth.handlers.shared.save_earth_utils import get_storage
    return json.loads(get_storage().read_text(res.absolute_path))


def test_mock_download_writes_valid_geojson(local_storage):
    from save_earth.handlers.shared.save_earth_utils import alpr

    res = alpr.download(use_mock=True, force=True)
    assert res.used_mock and not res.was_cached
    assert res.feature_count == 4
    fc = _load(res)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 4
    for f in fc["features"]:
        assert f["geometry"]["type"] == "Point"
        lon, lat = f["geometry"]["coordinates"]
        assert -180 <= lon <= 180 and -90 <= lat <= 90
        p = f["properties"]
        # every OSM tag kept verbatim + the ALPR tag convention
        assert p["man_made"] == "surveillance"
        assert p["surveillance:type"] == "ALPR"
        # derived provenance + vendor fields
        assert p["osm_url"].startswith("https://www.openstreetmap.org/node/")
        assert p["camera_vendor"] in {"flock", "motorola", "other"}


def test_vendor_classification(local_storage):
    from save_earth.handlers.shared.save_earth_utils import alpr

    fc = _load(alpr.download(use_mock=True, force=True))
    vendors = sorted(f["properties"]["camera_vendor"] for f in fc["features"])
    # mock set: 2 Flock, 1 Motorola/Vigilant, 1 untagged → other
    assert vendors == ["flock", "flock", "motorola", "other"]


def test_classify_vendor_unit():
    from save_earth.handlers.shared.save_earth_utils import alpr

    assert alpr._classify_vendor({"manufacturer": "Flock Safety"}) == "flock"
    assert alpr._classify_vendor({"brand": "Vigilant"}) == "motorola"
    assert alpr._classify_vendor({"manufacturer": "Motorola Solutions"}) == "motorola"
    assert alpr._classify_vendor({}) == "other"


def test_cache_short_circuits(local_storage):
    from save_earth.handlers.shared.save_earth_utils import alpr

    first = alpr.download(use_mock=True, force=True)
    assert not first.was_cached
    second = alpr.download(use_mock=True)  # within freshness window → cache hit
    assert second.was_cached
    assert second.feature_count == first.feature_count


def test_handler_dispatch(local_storage):
    from save_earth.handlers.sources import source_handlers

    out = source_handlers.handle({"_facet_name": "save_earth.sources.DownloadALPRCameras",
                                  "use_mock": True, "force": True})
    assert out["cache_type"] == "alpr"
    assert out["feature_count"] == 4
    assert out["used_mock"] is True
