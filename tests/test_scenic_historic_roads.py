"""Offline tests for the scenic + historic roads map (two toggle layers, LINES).

The network is never touched: `use_mock=True` for the cache/render path, and the
Overpass-shaped element dicts are fed to the pure converters directly.
"""

from __future__ import annotations

import json
import re

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


def _mod():
    from save_earth.handlers.shared.save_earth_utils import scenic_historic_roads
    return scenic_historic_roads


def test_mock_download_splits_by_road_kind(local_storage):
    from save_earth.handlers.shared.save_earth_utils import get_storage
    res = _mod().download(use_mock=True, force=True)
    fc = json.loads(get_storage().read_text(res.absolute_path))
    kinds = sorted(f["properties"]["road_kind"] for f in fc["features"])
    assert kinds == ["historic", "scenic"]
    for f in fc["features"]:
        assert f["geometry"]["type"] == "LineString"
        assert len(f["geometry"]["coordinates"]) >= 2


def test_cache_path_is_region_scoped():
    m = _mod()
    assert m.relative_path("north-america") != m.relative_path("world")
    assert "north-america" in m.relative_path("north-america")


def test_regions_config_has_bbox_and_world_is_unbounded():
    m = _mod()
    regs = m.regions()
    assert "north-america" in regs and "world" in regs
    na = m.region_config("north-america")
    assert len(na["bbox"]) == 4
    # A null bbox means "no bbox clause" -> worldwide.
    assert m._bbox_clause(m.region_config("world")) == ""
    assert m._bbox_clause(na).startswith("(")


def test_queries_never_use_a_bare_historic_key():
    """A bare `[historic]` makes Overpass scan every historic object (all the
    ruins/tombs/buildings) before intersecting — it blew a client timeout in
    testing. Every sub-query must pin a VALUE."""
    m = _mod()
    for _kind, q in m._queries("north-america"):
        assert '["historic"]' not in q
        assert '["scenic"]' not in q


def test_relation_members_are_gated_on_confirmed_road_ids():
    """route=historic also carries mistagged historic RAILWAYS; only members
    Overpass confirmed are [highway] may become features."""
    m = _mod()
    el = {
        "type": "relation", "id": 7, "tags": {"name": "Old MN 112", "route": "historic"},
        "members": [
            {"type": "way", "ref": 100, "geometry": [{"lat": 1.0, "lon": 2.0},
                                                     {"lat": 1.1, "lon": 2.1}]},
            {"type": "way", "ref": 200, "geometry": [{"lat": 3.0, "lon": 4.0},
                                                     {"lat": 3.1, "lon": 4.1}]},
        ],
    }
    feats = m._to_features(el, "historic", {100})
    assert len(feats) == 1
    assert feats[0]["properties"]["osm_id"] == "relation/7/100"
    # The RELATION carries the route name; a member way alone would not.
    assert feats[0]["properties"]["route_name"] == "Old MN 112"
    assert feats[0]["properties"]["name"] == "Old MN 112"


def test_transit_mistags_are_excluded():
    """Three Salem/Keizer bus routes really carry route=historic upstream."""
    m = _mod()
    bus = {"type": "relation", "id": 73895,
           "tags": {"name": "17 - Hayesville", "route": "historic",
                    "network": "Cherriots", "operator": "Salem/Keizer Transit"},
           "members": [{"type": "way", "ref": 1,
                        "geometry": [{"lat": 1.0, "lon": 2.0}, {"lat": 1.1, "lon": 2.1}]}]}
    assert m._to_features(bus, "historic", {1}) == []


def test_short_and_empty_geometry_is_dropped():
    m = _mod()
    assert m._line(None) is None
    assert m._line([{"lat": 1.0, "lon": 2.0}]) is None          # a single node is not a line
    assert len(m._line([{"lat": 1.0, "lon": 2.0}, {"lat": 1.1, "lon": 2.1}])) == 2


def test_coordinates_are_rounded():
    m = _mod()
    coords = m._line([{"lat": 1.123456789, "lon": 2.987654321},
                      {"lat": 1.2, "lon": 2.3}])
    assert coords[0] == [2.98765, 1.12346]      # lon,lat order, 5 dp


def test_two_toggle_layers_off_one_source(local_storage):
    from save_earth.handlers.maps import map_handlers
    from save_earth.handlers.sources import source_handlers
    source_handlers.handle({"_facet_name": "save_earth.sources.DownloadScenicHistoricRoads",
                            "region": "north-america", "use_mock": True, "force": True})
    out = map_handlers.handle_build_map({"region": "scenic-historic-roads",
                                         "only_layers": "roads-north-america-*"})
    counts = json.loads(out["layer_counts"])
    assert counts == {"roads-north-america-scenic": 1, "roads-north-america-historic": 1}
    html = open(out["html_path"]).read()
    specs = json.loads(re.search(r"const LAYER_SPECS = (\[.*?\]);", html, re.S).group(1))
    assert len({s["source_id"] for s in specs}) == 1     # ONE shared cached file
    assert {s["geometry"] for s in specs} == {"line"}
    # The split is what makes two checkboxes out of one file.
    assert {s["filter_field"] for s in specs} == {"road_kind"}
    assert {s["filter_value"] for s in specs} == {"scenic", "historic"}


def test_default_basemap_is_keyless_and_style_form():
    """CARTO's tiles now carry an "API KEY REQUIRED" watermark while still
    returning 200 OK, so the default must not point at them."""
    from save_earth.handlers.shared.save_earth_utils import map_render
    url, attr = map_render.default_basemap()
    assert "cartodb" not in url and "carto.com" not in attr
    # A style URL, not a raster template -> handed to MapLibre directly.
    assert not map_render.is_raster_basemap(url)


def test_raster_basemap_still_supported(monkeypatch):
    """A deployment with its own tile server must still work."""
    from save_earth.handlers.shared.save_earth_utils import map_render
    monkeypatch.setenv("FW_BASEMAP_URL", "https://tiles.example/{z}/{x}/{y}.png")
    url, _ = map_render.default_basemap()
    assert map_render.is_raster_basemap(url)
