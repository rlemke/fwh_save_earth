"""Offline test for the ALPR vendor-split + density-heatmap renderer wiring.

Verifies the renderer capabilities the ALPR map relies on: several LayerSpecs
sharing ONE cached source (inlined once, not duplicated), per-vendor property
filters, filtered feature counts, and a heatmap geometry.
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


def test_alpr_vendor_split_and_heatmap(local_storage):
    from save_earth.handlers.maps import map_handlers
    from save_earth.handlers.sources import source_handlers

    source_handlers.handle({"_facet_name": "save_earth.sources.DownloadALPRCameras",
                            "use_mock": True, "force": True})
    out = map_handlers.handle_build_map({"region": "alpr", "only_layers": "alpr-*",
                                         "max_inline_features": 130000})

    counts = json.loads(out["layer_counts"])
    # heatmap over all 4; vendor circles filtered 2 flock / 1 motorola / 1 other
    assert counts == {"alpr-heatmap": 4, "alpr-flock": 2,
                      "alpr-motorola": 1, "alpr-other": 1}

    html = open(out["html_path"]).read()
    # ONE inlined source shared by all 4 layers (not duplicated per layer)
    data_keys = list(json.loads(re.search(r"const LAYER_DATA = (\{.*?\});", html, re.S).group(1)))
    assert len(data_keys) == 1

    specs = json.loads(re.search(r"const LAYER_SPECS = (\[.*?\]);", html, re.S).group(1))
    by_name = {s["name"]: s for s in specs}
    assert by_name["alpr-heatmap"]["geometry"] == "heatmap"
    assert by_name["alpr-flock"]["filter_field"] == "camera_vendor"
    assert by_name["alpr-flock"]["filter_value"] == "flock"
    # all four specs point at the same shared source id
    assert len({s["source_id"] for s in specs}) == 1
    # heatmap layer type + filter expression are emitted in the JS
    assert "type: 'heatmap'" in html
    assert "['to-string', ['get', spec.filter_field]]" in html


def test_max_inline_features_default_unchanged(local_storage):
    # A single-layer map (nuclear) still renders with the default cap.
    from save_earth.handlers.maps import map_handlers
    from save_earth.handlers.sources import source_handlers

    source_handlers.handle({"_facet_name": "save_earth.sources.DownloadNuclearReactors",
                            "use_mock": True, "force": True})
    out = map_handlers.handle_build_map({"region": "nuclear", "only_layers": "nuclear-reactors"})
    assert json.loads(out["layer_counts"])["nuclear-reactors"] > 0
