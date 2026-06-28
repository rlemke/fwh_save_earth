"""Offline tests for the renewable-energy siting annotation.

Fully offline: NASA POWER's regional endpoint is monkeypatched, the source
solar/wind plant layers are written into a temp local cache, and
``siting.annotate`` is exercised end to end. Regressions guarded: (1) only the
10° tiles that actually contain plants are fetched (one call per tile per
parameter), (2) the raw resource value + ``siting_score`` (4..8) land on each
plant from the right grid, (3) no-data tiles leave the plant unscored rather
than crashing, (4) the cache short-circuits re-sampling.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
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


def _fake_regional(fetched):
    """A fake _regional that returns a full synthetic 1° grid for any 10° tile and
    records which (param, tile) were fetched."""
    def regional(param, tlon, tlat, **_):
        fetched.append((param, (tlon, tlat)))
        val = 6.0 if param == "ALLSKY_SFC_SW_DWN" else 9.5
        return [(tlon + i + 0.5, tlat + j + 0.5, val)
                for i in range(10) for j in range(10)]
    return regional


def test_siting_score_mapping():
    from save_earth.handlers.shared.save_earth_utils import siting

    # solar domain (2.5, 6.5) -> [4, 8]
    assert siting._siting_score(2.5, 2.5, 6.5) == 4.0
    assert siting._siting_score(6.5, 2.5, 6.5) == 8.0
    assert siting._siting_score(4.5, 2.5, 6.5) == 6.0
    # clamps outside the domain
    assert siting._siting_score(1.0, 2.5, 6.5) == 4.0
    assert siting._siting_score(9.0, 2.5, 6.5) == 8.0


def test_only_plant_tiles_fetched_and_annotated(local_storage, monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import siting

    # Two solar plants in the SAME 10° tile (-120,30) + one in tile (0,40);
    # one wind plant in tile (-10,50).
    _write_plants("solar", [(-115.2, 35.3), (-114.8, 36.1), (8.0, 48.0)])
    _write_plants("wind", [(-3.0, 56.0)])

    fetched: list = []
    monkeypatch.setattr(siting, "_regional", _fake_regional(fetched))

    res = siting.annotate(force=True, max_workers=1)

    assert res.feature_count == 4
    # solar -> GHI over 2 tiles; wind -> WS50M over 1 tile. No extra calls.
    assert sorted(fetched) == sorted([
        ("ALLSKY_SFC_SW_DWN", (-120, 30)),
        ("ALLSKY_SFC_SW_DWN", (0, 40)),
        ("WS50M", (-10, 50)),
    ])

    solar = _read_sited("siting_solar.geojson")
    assert len(solar) == 3
    for ft in solar:
        assert ft["properties"]["ghi_kwh_m2_day"] == 6.0
        assert ft["properties"]["siting_score"] == siting._siting_score(6.0, 2.5, 6.5)
        assert ft["properties"]["name"]  # original WRI fields preserved

    wind = _read_sited("siting_wind.geojson")
    assert len(wind) == 1
    assert wind[0]["properties"]["wind_speed_ms"] == 9.5


def test_no_data_tile_leaves_plant_unscored(local_storage, monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import siting

    _write_plants("solar", [(0.0, 0.0)])
    _write_plants("wind", [])
    monkeypatch.setattr(siting, "_regional", lambda param, tlon, tlat, **_: [])  # no-data

    res = siting.annotate(force=True, max_workers=1)
    assert res.feature_count == 1
    ft = _read_sited("siting_solar.geojson")[0]
    assert "ghi_kwh_m2_day" not in ft["properties"]
    assert "siting_score" not in ft["properties"]
    assert ft["properties"]["name"]  # plant still emitted, just unscored


def test_cache_aware_skips_resampling(local_storage, monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import siting

    _write_plants("solar", [(-115.0, 35.0)])
    _write_plants("wind", [(-3.0, 56.0)])
    monkeypatch.setattr(siting, "_regional", _fake_regional([]))

    first = siting.annotate(force=True, max_workers=1)
    assert first.was_cached is False
    assert first.feature_count == 2

    def boom(*a, **k):  # must NOT be called on the cached path
        raise AssertionError("NASA POWER hit on a cached run")

    monkeypatch.setattr(siting, "_regional", boom)
    second = siting.annotate(force=False, max_workers=1)
    assert second.was_cached is True
    assert second.feature_count == 2
