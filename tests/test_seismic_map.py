"""Offline tests for the seismic sources + map render (earthquakes + faults).

Fully offline: uses each source's ``use_mock=True`` path and a temp local
storage backend — no network, no MongoDB. Verifies the two new sources cache
GeoJSON, and that the map handler renders both a magnitude-styled circle layer
(earthquakes) and a line layer (fault/plate boundaries) into one HTML bundle.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    # The tools cache the get_storage() singleton lazily off the env, so import
    # inside the test (after the env is set) — see the other source modules.
    yield tmp_path


def test_mock_earthquakes_and_faults_download(local_storage):
    from save_earth.handlers.shared.save_earth_utils import seismic

    q = seismic.download_earthquakes(use_mock=True)
    f = seismic.download_faults(use_mock=True)

    assert q.feature_count >= 1 and q.used_mock
    assert f.feature_count >= 1 and f.used_mock
    assert q.relative_path == seismic.EARTHQUAKES_RELATIVE_PATH
    assert f.relative_path == seismic.FAULTS_RELATIVE_PATH


def test_quake_features_carry_magnitude_and_depth(local_storage):
    import json

    from save_earth.handlers.shared.save_earth_utils import seismic
    from save_earth.handlers.shared.save_earth_utils import get_storage

    res = seismic.download_earthquakes(use_mock=True)
    fc = json.loads(get_storage().read_text(res.absolute_path))
    props = fc["features"][0]["properties"]
    assert isinstance(props.get("mag"), (int, float))  # drives circle size/colour
    assert "depth_km" in props
    assert fc["features"][0]["geometry"]["type"] == "Point"


def test_fault_features_are_lines(local_storage):
    import json

    from save_earth.handlers.shared.save_earth_utils import seismic
    from save_earth.handlers.shared.save_earth_utils import get_storage

    res = seismic.download_faults(use_mock=True)
    fc = json.loads(get_storage().read_text(res.absolute_path))
    assert fc["features"][0]["geometry"]["type"] in ("LineString", "MultiLineString")


def test_build_seismic_map_renders_both_layers(local_storage):
    import json

    from save_earth.handlers.shared.save_earth_utils import seismic
    from save_earth.handlers.maps import map_handlers

    seismic.download_faults(use_mock=True)
    seismic.download_earthquakes(use_mock=True)

    out = map_handlers.handle_build_map(
        {
            "region": "seismic",
            "only_layers": "faults,earthquakes",
            "zoom": 1.5,
            "attribution_workflow": "save_earth.workflows.BuildSeismicMap",
            "attribution_ffl_url": "https://example/save_earth.ffl",
            "description": "test",
        }
    )
    counts = json.loads(out["layer_counts"])
    assert out["layer_count"] == 2
    assert counts.get("faults", 0) >= 1
    assert counts.get("earthquakes", 0) >= 1

    from save_earth.handlers.shared.save_earth_utils import get_storage

    html = get_storage().read_text(out["html_path"])
    # line geometry for faults + magnitude-driven circle styling for quakes
    assert "type: 'line'" in html
    assert "colorExpr(spec)" in html and "radiusExpr(spec)" in html
    assert "magnitude_field" in html


def test_tesla_mock_download_and_layer(local_storage):
    from save_earth.handlers.shared.save_earth_utils import tesla
    from save_earth.handlers.maps import map_handlers as mh

    res = tesla.download(use_mock=True)
    assert res.feature_count == 3 and res.used_mock
    assert res.relative_path == tesla.RELATIVE_PATH
    # the tesla layer is in the map candidates
    assert any(getattr(L, "name", "") == "tesla" for L in
               [mh._TESLA_LAYER])
    assert mh._TESLA_LAYER.source_cache_type == "tesla"


def test_telescope_mock_download_and_layer(local_storage):
    from save_earth.handlers.shared.save_earth_utils import telescope
    from save_earth.handlers.maps import map_handlers as mh

    res = telescope.download(use_mock=True)
    assert res.feature_count == 4 and res.used_mock
    assert res.relative_path == telescope.RELATIVE_PATH
    assert mh._TELESCOPE_LAYER.source_cache_type == "telescopes"
