"""Offline tests for the data-center + aquifer sources and the fill-layer map."""

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


def test_datacenters_mock_points(local_storage):
    from save_earth.handlers.shared.save_earth_utils import datacenters
    res = datacenters.download(use_mock=True, force=True)
    fc = _load(res)
    assert res.feature_count == 3
    for f in fc["features"]:
        assert f["geometry"]["type"] == "Point"
        assert f["properties"]["osm_url"].startswith("https://www.openstreetmap.org/")


def test_aquifers_mock_polygons(local_storage):
    from save_earth.handlers.shared.save_earth_utils import aquifers
    res = aquifers.download(use_mock=True, force=True)
    fc = _load(res)
    assert res.feature_count == 2
    assert all(f["geometry"]["type"] == "Polygon" for f in fc["features"])
    assert all(f["properties"].get("AQ_NAME") for f in fc["features"])


def test_datacenter_water_map_renders_fill_and_points(local_storage):
    from save_earth.handlers.maps import map_handlers
    from save_earth.handlers.sources import source_handlers
    for facet in ("DownloadDataCenters", "DownloadAquifers"):
        source_handlers.handle({"_facet_name": f"save_earth.sources.{facet}",
                                "use_mock": True, "force": True})
    out = map_handlers.handle_build_map({"region": "data-centers",
                                         "only_layers": "aquifers,data-centers"})
    counts = json.loads(out["layer_counts"])
    assert counts == {"aquifers": 2, "data-centers": 3}
    html = open(out["html_path"]).read()
    assert "type: 'fill'" in html          # aquifer polygons
    assert "'circle'" in html or "type: 'circle'" in html
