"""Offline tests for the NIFC/WFIGS fire-perimeter source.

Fully offline via ``use_mock=True``. Pins the three things that would quietly
misrepresent the map:

* a fully-CONTAINED fire must not be indistinguishable from an active one — the
  "Current" feed carries both (60 of 231 observed perimeters were 100% contained);
* absent containment data must NOT be read as contained;
* the layers must be `fill` geometry drawn BENEATH the detection points, or the
  polygons hide the dots.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


def test_mock_perimeter_download(local_storage):
    from save_earth.handlers.shared.save_earth_utils import fire_perimeters as fp

    res = fp.download_perimeters(use_mock=True)
    assert res.used_mock and res.feature_count == 3
    assert res.relative_path == fp.RELATIVE_PATH
    assert sum((res.status_counts or {}).values()) == res.feature_count
    assert res.acres_total > 0


def test_status_derivation_never_treats_missing_data_as_contained():
    """Absent containment is 'unreported', not 'contained' — the safe direction."""
    from save_earth.handlers.shared.save_earth_utils import fire_perimeters as fp

    assert fp._status_of(100) == "contained"
    assert fp._status_of(100.0) == "contained"
    assert fp._status_of(0) == "active"
    assert fp._status_of(45) == "active"
    assert fp._status_of(None) == "unreported"
    assert fp._status_of("") == "unreported"
    assert fp._status_of("garbage") == "unreported"


def test_features_carry_incident_identity(local_storage):
    """The whole point of perimeters vs detections: a named, measured incident."""
    from save_earth.handlers.shared.save_earth_utils import fire_perimeters as fp
    from save_earth.handlers.shared.save_earth_utils import get_storage

    res = fp.download_perimeters(use_mock=True)
    fc = json.loads(get_storage().read_text(res.absolute_path))
    for feat in fc["features"]:
        p = feat["properties"]
        assert p["incident_name"]
        assert p["status"] in ("active", "contained", "unreported")
        assert feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
    # US-only coverage must be stated in the collection, not just the docs.
    assert "UNITED STATES ONLY" in fc["properties"]["note"]


def test_perimeter_layers_are_fill_and_drawn_under_the_points():
    from save_earth.handlers.maps.map_handlers import _FIRE_LAYERS, _PERIMETER_LAYERS

    assert len(_PERIMETER_LAYERS) == 3
    for layer in _PERIMETER_LAYERS:
        assert layer.geometry == "fill", "polygons must not render as circles"
        assert layer.filter_field == "status"
        # Shares ONE cached file across the three status layers.
        assert layer.source_cache_type == "fire_perimeters"
    statuses = {layer.filter_value for layer in _PERIMETER_LAYERS}
    assert statuses == {"active", "contained", "unreported"}
    # `only_layers="fire-*"` must select perimeters AND detections together.
    for layer in _PERIMETER_LAYERS + _FIRE_LAYERS:
        assert layer.name.startswith("fire-")


def test_map_renders_perimeters_beneath_detections(local_storage):
    import re

    from save_earth.handlers.maps import map_handlers as mh
    from save_earth.handlers.shared.save_earth_utils import fire_perimeters as fp
    from save_earth.handlers.shared.save_earth_utils import wildfire

    wildfire.download_active_fire(use_mock=True)
    fp.download_perimeters(use_mock=True)
    out = mh.handle_build_map({
        "_facet_name": "save_earth.maps.BuildMap",
        "region": "perim-order",
        "only_layers": "fire-*",
    })
    html = open(out["html_path"]).read()
    specs = json.loads(re.search(r"const LAYER_SPECS = (\[.*?\]);", html, re.S).group(1))
    ids = [s["id"] for s in specs]
    last_perimeter = max(i for i, x in enumerate(ids) if "perimeter" in x)
    first_point = min(i for i, x in enumerate(ids) if "perimeter" not in x)
    assert last_perimeter < first_point, f"points must draw on top of polygons: {ids}"


def test_us_region_scoping_beats_the_global_cap():
    """A US map built from the WORLD file keeps only 24% of US detections.

    The world collection is FRP-sorted globally and the renderer's cap is a plain
    slice, so global rank decides what survives: measured 2,701 of 11,092 US
    detections. A region gets its own cache entry so the cap applies to ITS data.
    """
    from save_earth.handlers.shared.save_earth_utils import wildfire

    assert wildfire.relative_path_for("world") == wildfire.RELATIVE_PATH
    assert wildfire.relative_path_for("us") == "active_fire_us.geojson"
    assert wildfire.relative_path_for("us") != wildfire.relative_path_for("world")
    with pytest.raises(ValueError):
        wildfire.relative_path_for("atlantis")


def test_us_layers_read_the_region_scoped_file():
    from save_earth.handlers.maps.map_handlers import _FIRE_LAYERS, _US_FIRE_LAYERS
    from save_earth.handlers.shared.save_earth_utils import wildfire

    assert len(_US_FIRE_LAYERS) == 3
    for layer in _US_FIRE_LAYERS:
        assert layer.source_relative_path == wildfire.relative_path_for("us")
        assert layer.name.startswith("usfire-")
    # The world layers must NOT be selected by the US map's only_layers, and vice
    # versa — that separation is the whole point of the region scoping.
    for layer in _FIRE_LAYERS:
        assert layer.source_relative_path == wildfire.relative_path_for("world")
        assert not layer.name.startswith("usfire-")


def test_us_bbox_filter_excludes_non_us_points():
    from save_earth.handlers.shared.save_earth_utils import wildfire

    box = wildfire.REGIONS["us"]
    mk = lambda lon, lat: {"geometry": {"coordinates": [lon, lat]}}
    assert wildfire._in_bbox(mk(-120.5, 38.7), box)   # California
    assert wildfire._in_bbox(mk(-150.0, 61.0), box)   # Alaska
    assert wildfire._in_bbox(mk(-156.0, 19.5), box)   # Hawaii
    assert not wildfire._in_bbox(mk(133.9, -23.4), box)   # Australia
    assert not wildfire._in_bbox(mk(25.1, -12.1), box)    # Zambia
    assert not wildfire._in_bbox(mk(-3.7, 40.4), box)     # Spain
