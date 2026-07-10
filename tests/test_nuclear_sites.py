"""Offline tests for the nuclear test-sites + missile-silos map (two toggle layers)."""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


def test_nuclear_sites_mock_split(local_storage):
    from save_earth.handlers.shared.save_earth_utils import nuclear_sites
    res = nuclear_sites.download(use_mock=True, force=True)
    from save_earth.handlers.shared.save_earth_utils import get_storage
    fc = json.loads(get_storage().read_text(res.absolute_path))
    types = sorted(f["properties"]["site_type"] for f in fc["features"])
    assert types == ["missile_silo", "missile_silo", "test_site", "test_site"]
    for f in fc["features"]:
        assert f["geometry"]["type"] == "Point"


def test_two_toggle_layers_one_source(local_storage):
    from save_earth.handlers.maps import map_handlers
    from save_earth.handlers.sources import source_handlers
    source_handlers.handle({"_facet_name": "save_earth.sources.DownloadNuclearSites",
                            "use_mock": True, "force": True})
    out = map_handlers.handle_build_map({"region": "nuclear-sites",
                                         "only_layers": "nuclear-test-sites,missile-silos"})
    counts = json.loads(out["layer_counts"])
    assert counts == {"nuclear-test-sites": 2, "missile-silos": 2}
    import re
    html = open(out["html_path"]).read()
    specs = json.loads(re.search(r"const LAYER_SPECS = (\[.*?\]);", html, re.S).group(1))
    assert len({s["source_id"] for s in specs}) == 1     # one shared cached file
    assert {s["filter_value"] for s in specs} == {"test_site", "missile_silo"}
