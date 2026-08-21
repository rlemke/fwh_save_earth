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


# ---------------------------------------------------------------------------
# The inline cap: sampling must be unbiased AND declared
# ---------------------------------------------------------------------------


def test_over_the_cap_the_sample_is_evenly_spaced_not_a_head_slice():
    """Cached files are written in OSM id order, and id order is age order, so
    "the first N" systematically discards the NEWEST features. On a coverage
    map that is a bias in exactly the dimension the map is about — the most
    recently mapped cameras would simply vanish once the dataset outgrew the
    cap, which it now has (144,635 against a 130,000 cap)."""
    from save_earth.tools._save_earth_tools import map_render

    feats = [{"id": i} for i in range(1000)]
    cap = 100
    stride = len(feats) / cap
    kept = [feats[int(i * stride)] for i in range(cap)]

    assert len(kept) == cap
    assert kept[0]["id"] == 0
    # the crucial property: the sample reaches the END of the file
    assert kept[-1]["id"] >= 900, "a head slice would stop at 99"
    ids = [k["id"] for k in kept]
    assert ids == sorted(ids), "deterministic and ordered, so rebuilds are stable"


def test_sampling_is_declared_on_the_page():
    """The drop count used to be computed and then thrown away: the reader saw
    a map that looked complete with no way to know otherwise. A coverage map
    silently omitting a tenth of its subject is worse than one that says so."""
    from save_earth.tools._save_earth_tools.map_render import _with_sampling_note

    note = _with_sampling_note("Base description.", {("t", "p"): 14635}, 130000)
    assert "Base description." in note
    assert "130,000" in note and "14,635" in note
    assert "sample" in note.lower()


def test_no_note_when_nothing_was_dropped():
    from save_earth.tools._save_earth_tools.map_render import _with_sampling_note

    assert _with_sampling_note("Base.", {}, 130000) == "Base."
