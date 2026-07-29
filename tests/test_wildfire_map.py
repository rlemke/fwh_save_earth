"""Offline tests for the FIRMS active-fire source + the thermal-anomaly map.

Fully offline: uses ``use_mock=True`` and a temp local storage backend — no
network, no MongoDB. Covers the three things this layer gets wrong most easily:

* the two sensors' different confidence scales must land on ONE band property,
  or the map's band filters silently drop a sensor;
* the cached collection must be FRP-sorted, because the renderer's inline cap is
  a plain slice — the sort is what decides which detections a capped map keeps;
* ``layer_counts`` must describe what is DRAWN, not what is cached. It used to
  report pre-cap totals, so a capped map overstated itself 3.3x.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


def test_mock_active_fire_download(local_storage):
    from save_earth.handlers.shared.save_earth_utils import wildfire

    res = wildfire.download_active_fire(use_mock=True)
    assert res.used_mock and res.feature_count >= 1
    assert res.relative_path == wildfire.RELATIVE_PATH
    # Bands are reported without re-reading the (large) collection.
    assert set(res.band_counts or {}) <= {"low", "nominal", "high"}
    assert sum((res.band_counts or {}).values()) == res.feature_count


def test_features_carry_normalised_cross_sensor_properties(local_storage):
    from save_earth.handlers.shared.save_earth_utils import get_storage, wildfire

    res = wildfire.download_active_fire(use_mock=True)
    fc = json.loads(get_storage().read_text(res.absolute_path))
    for feat in fc["features"]:
        props = feat["properties"]
        # The map filters on confidence_band and sizes circles by frp.
        assert props["confidence_band"] in ("low", "nominal", "high")
        assert isinstance(props["frp"], (int, float))
        assert props["sensor"] in ("VIIRS", "MODIS")
        assert props["daynight"] in ("day", "night")
        # Per-sensor constants must NOT be repeated per feature (12 MB of waste
        # at real volumes) — they live in the collection header instead.
        assert "platform" not in props
        assert "resolution_m" not in props
    assert "VIIRS" in (fc["properties"].get("sensors") or {})


def test_modis_percentage_confidence_bands_like_viirs_categories():
    """MODIS reports 0-100, VIIRS reports low/nominal/high — one property out."""
    from save_earth.handlers.shared.save_earth_utils import wildfire

    assert wildfire._band("10", "MODIS") == "low"
    assert wildfire._band("50", "MODIS") == "nominal"
    assert wildfire._band("95", "MODIS") == "high"
    assert wildfire._band("high", "VIIRS") == "high"
    # Unparseable confidence must not crash a 130k-row ingest.
    assert wildfire._band("", "MODIS") == "nominal"
    assert wildfire._band("garbage", "VIIRS") == "nominal"


def test_features_are_sorted_by_frp_descending(local_storage):
    """The renderer's cap is a plain slice, so sort order decides what survives."""
    from save_earth.handlers.shared.save_earth_utils import get_storage, wildfire

    res = wildfire.download_active_fire(use_mock=True)
    fc = json.loads(get_storage().read_text(res.absolute_path))
    frps = [f["properties"]["frp"] for f in fc["features"]]
    assert frps == sorted(frps, reverse=True)


def test_acquisition_window_is_reported(local_storage):
    """A near-real-time map that cannot date itself invites 'this is live'."""
    from save_earth.handlers.shared.save_earth_utils import wildfire

    res = wildfire.download_active_fire(use_mock=True)
    assert res.acquired_from and res.acquired_to
    assert res.acquired_from <= res.acquired_to


def test_map_renders_three_confidence_bands_from_one_source(local_storage):
    from save_earth.handlers.maps import map_handlers as mh
    from save_earth.handlers.shared.save_earth_utils import wildfire

    wildfire.download_active_fire(use_mock=True)
    out = mh.handle_build_map({
        "_facet_name": "save_earth.maps.BuildMap",
        "region": "wildfire-test",
        "only_layers": "fire-*",
    })
    counts = out["layer_counts"]
    if isinstance(counts, str):
        counts = json.loads(counts)
    assert set(counts) == {"fire-low", "fire-nominal", "fire-high"}
    # The mock has exactly one detection per band, which also proves the three
    # layers really are filtering ONE shared cached file rather than each
    # inlining the whole collection.
    assert all(v == 1 for v in counts.values())


def test_layer_counts_reflect_what_is_drawn_not_what_is_cached(local_storage):
    """Regression: counts were computed pre-cap, overstating a capped map."""
    import re

    from save_earth.handlers.maps import map_handlers as mh
    from save_earth.handlers.shared.save_earth_utils import wildfire

    wildfire.download_active_fire(use_mock=True)
    out = mh.handle_build_map({
        "_facet_name": "save_earth.maps.BuildMap",
        "region": "wildfire-capped",
        "only_layers": "fire-*",
        "max_inline_features": 2,  # mock has 3 detections
    })
    counts = out["layer_counts"]
    if isinstance(counts, str):
        counts = json.loads(counts)
    assert sum(counts.values()) == 2, "counts must not exceed the inline cap"

    html = open(out["html_path"]).read()
    data = json.loads(re.search(r"const LAYER_DATA = (\{.*?\});\n", html, re.S).group(1))
    inlined = sum(len(v.get("features", [])) for v in data.values())
    assert inlined == sum(counts.values()), "reported counts must equal inlined features"


def test_fire_layers_use_frp_stops_not_earthquake_stops(local_storage):
    """The magnitude scale defaults to Richter 4/6/8 -> 3/9/22 px.

    FRP runs 0-4000 MW, so on those defaults EVERY detection above 8 MW pinned at
    the 22 px cap -- measured at 100% of drawn dots, median radius 22 px, which
    rendered the map as one solid blob. The fire layers must therefore carry
    their own stops, and must NOT take the magnitude colour ramp: their colour
    already encodes confidence, and the ramp overriding it made the legend
    disagree with the dots.
    """
    from save_earth.handlers.maps.map_handlers import _FIRE_LAYERS

    for layer in _FIRE_LAYERS:
        assert layer.magnitude_field == "frp"
        assert layer.magnitude_stops != (4.0, 6.0, 8.0), "still on the earthquake stops"
        assert max(layer.magnitude_radii) <= 12, "dots would dominate the map"
        assert layer.magnitude_color is False, "confidence colour must win"


def test_earthquake_layer_keeps_the_defaults(local_storage):
    """The parameterisation must not disturb the map it was tuned for."""
    from save_earth.handlers.maps.map_handlers import _EARTHQUAKE_LAYER

    assert _EARTHQUAKE_LAYER.magnitude_stops == (4.0, 6.0, 8.0)
    assert _EARTHQUAKE_LAYER.magnitude_radii == (3.0, 9.0, 22.0)
    assert _EARTHQUAKE_LAYER.magnitude_color is True
