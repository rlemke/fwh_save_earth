"""Offline tests for the hydroelectric dam map (four type layers, one source)."""

from __future__ import annotations

import json
import re

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


@pytest.mark.parametrize(
    ("raw", "mw"),
    [
        ("22500 MW", 22500.0),
        ("14 GW", 14000.0),
        ("2,08 GW", 2080.0),  # decimal comma
        ("1,200 MW", 1200.0),  # thousands separator
        ("500 kW", 0.5),
        ("24MW", 24.0),
        ("yes", None),
        ("", None),
        (None, None),
        ("5 MW;3 MW", None),  # not one quantity — not guessed
    ],
)
def test_parse_capacity_mw(raw, mw):
    from save_earth.handlers.shared.save_earth_utils import hydro

    assert hydro.parse_capacity_mw(raw) == mw


def test_type_comes_only_from_plant_method():
    from save_earth.handlers.shared.save_earth_utils import hydro

    assert hydro.hydro_type({"plant:method": "water-storage"}) == "storage"
    assert hydro.hydro_type({"plant:method": "Run-of-the-river"}) == "run_of_river"
    assert hydro.hydro_type({"plant:method": "water-pumped-storage"}) == "pumped_storage"
    # A name or a dam tag is not evidence of the method.
    assert hydro.hydro_type({"name": "Big Reservoir Dam"}) == "unspecified"


def test_overpass_truncation_is_not_cached(monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import hydro

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": [], "remark": "runtime error: Query timed out"}

    monkeypatch.setattr(hydro.requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError, match="timed out"):
        hydro._fetch_overpass()


def test_map_has_one_layer_per_type_sharing_one_source(local_storage):
    from save_earth.handlers.maps import map_handlers
    from save_earth.handlers.sources import source_handlers

    got = source_handlers.handle(
        {"_facet_name": "save_earth.sources.DownloadHydroPlants", "use_mock": True, "force": True}
    )
    assert got["feature_count"] == 6 and got["used_mock"]
    out = map_handlers.handle_build_map({"region": "hydro", "only_layers": "hydro-*"})
    counts = json.loads(out["layer_counts"])
    assert counts == {
        "hydro-storage": 3,
        "hydro-run-of-river": 1,
        "hydro-pumped-storage": 1,
        "hydro-unspecified": 1,
    }
    html = open(out["html_path"]).read()
    specs = json.loads(re.search(r"const LAYER_SPECS = (\[.*?\]);", html, re.S).group(1))
    assert len({s["source_id"] for s in specs}) == 1
    assert all(s["magnitude_field"] == "capacity_mw" for s in specs)
    assert all("capacity_mw" in s["description_fields"] for s in specs)


def test_derived_label_does_not_clobber_osm_type_tag():
    from save_earth.handlers.shared.save_earth_utils import hydro

    f = hydro.to_feature({"type": "relation", "id": 9, "center": {"lat": 1.0, "lon": 2.0},
                          "tags": {"type": "multipolygon", "plant:source": "hydro"}})
    assert f["properties"]["type"] == "multipolygon"
    assert f["properties"]["plant_type"] == "Type not recorded"
