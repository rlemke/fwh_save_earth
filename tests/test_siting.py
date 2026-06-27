"""Offline tests for the renewable-energy siting annotation.

Fully offline: NASA POWER is monkeypatched, the source solar/wind plant layers
are written into a temp local cache, and ``siting.annotate`` is exercised end to
end. Regressions guarded: (1) grid de-duplication collapses nearby plants to one
NASA POWER call, (2) the raw resource value + ``siting_score`` (4..8) land on
each plant, (3) missing/no-data cells leave the plant unscored rather than
crashing.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("AFL_STORAGE", "local")
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path))
    yield tmp_path


def _write_plants(slug, coords):
    """Write a power-cache GeoJSON layer of point plants at ``coords``."""
    from save_earth.handlers.shared.save_earth_utils import get_storage, power, sidecar

    s = get_storage()
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [lon, lat]},
              "properties": {"name": f"{slug}-{i}", "capacity_mw": "100"}}
             for i, (lon, lat) in enumerate(coords)]
    path = sidecar.cache_path("save-earth", power.CACHE_TYPE, f"{slug}.geojson", s)
    s.write_text_atomic(path, json.dumps({"type": "FeatureCollection", "features": feats}))


def _read_sited(rel):
    from save_earth.handlers.shared.save_earth_utils import get_storage, sidecar
    s = get_storage()
    path = sidecar.cache_path("save-earth", "siting", rel, s)
    return json.loads(s.read_text(path))["features"]


def test_siting_score_mapping():
    from save_earth.handlers.shared.save_earth_utils import siting

    # solar domain (2.5, 6.5) -> [4, 8]
    assert siting._siting_score(2.5, 2.5, 6.5) == 4.0
    assert siting._siting_score(6.5, 2.5, 6.5) == 8.0
    assert siting._siting_score(4.5, 2.5, 6.5) == 6.0
    # clamps outside the domain
    assert siting._siting_score(1.0, 2.5, 6.5) == 4.0
    assert siting._siting_score(9.0, 2.5, 6.5) == 8.0


def test_grid_dedup_and_annotation(local_storage, monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import siting

    # Two solar plants in the SAME 0.5deg cell + one far away; two wind plants.
    _write_plants("solar", [(-115.0, 35.0), (-115.1, 35.05), (10.0, 48.0)])
    _write_plants("wind", [(-115.05, 35.02), (-3.0, 56.0)])

    calls = []

    def fake_power(lat, lon, params):
        calls.append((round(lon, 4), round(lat, 4)))
        # Good solar near -115/35, good wind near -3/56, mediocre elsewhere.
        return {"ALLSKY_SFC_SW_DWN": 6.0, "WS50M": 9.5}

    monkeypatch.setattr(siting, "_nasa_power", fake_power)

    res = siting.annotate(force=True, grid_deg=0.5, throttle_s=0)

    # 3 solar + 2 wind = 5 plants, but their locations collapse to 3 unique cells
    # ((-115,35) shared by 3 plants, (10,48), (-3,56)).
    assert res.feature_count == 5
    assert res.cells_sampled == 3
    assert len(calls) == 3
    assert res.was_cached is False

    solar = _read_sited("siting_solar.geojson")
    assert len(solar) == 3
    for ft in solar:
        assert ft["properties"]["ghi_kwh_m2_day"] == 6.0
        assert ft["properties"]["siting_score"] == siting._siting_score(6.0, 2.5, 6.5)
        assert ft["properties"]["name"]  # original WRI fields preserved

    wind = _read_sited("siting_wind.geojson")
    assert len(wind) == 2
    assert wind[0]["properties"]["wind_speed_ms"] == 9.5


def test_no_data_cell_leaves_plant_unscored(local_storage, monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import siting

    _write_plants("solar", [(0.0, 0.0)])
    _write_plants("wind", [])
    monkeypatch.setattr(siting, "_nasa_power", lambda lat, lon, params: {})  # no-data

    res = siting.annotate(force=True, grid_deg=0.5, throttle_s=0)
    assert res.feature_count == 1
    ft = _read_sited("siting_solar.geojson")[0]
    assert "ghi_kwh_m2_day" not in ft["properties"]
    assert "siting_score" not in ft["properties"]
    assert ft["properties"]["name"]  # plant still emitted, just unscored


def test_cache_aware_skips_resampling(local_storage, monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import siting

    _write_plants("solar", [(-115.0, 35.0)])
    _write_plants("wind", [(-3.0, 56.0)])
    monkeypatch.setattr(siting, "_nasa_power",
                        lambda lat, lon, params: {"ALLSKY_SFC_SW_DWN": 6.0, "WS50M": 9.5})

    first = siting.annotate(force=True, grid_deg=0.5, throttle_s=0)
    assert first.was_cached is False

    def boom(*a, **k):  # must NOT be called on the cached path
        raise AssertionError("NASA POWER hit on a cached run")

    monkeypatch.setattr(siting, "_nasa_power", boom)
    second = siting.annotate(force=False, grid_deg=0.5, throttle_s=0)
    assert second.was_cached is True
    assert second.feature_count == 2
