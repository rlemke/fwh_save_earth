"""Offline tests for the ALPR (DeFlock) surveillance-camera source adapter.

Fully offline via ``use_mock=True`` (no Overpass, no network): exercises the
download → cache → sidecar path, the GeoJSON shape, the verbatim-tags +
derived-field contract (osm_url, camera_vendor), and the cache short-circuit.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


def _load(res):
    from save_earth.handlers.shared.save_earth_utils import get_storage
    return json.loads(get_storage().read_text(res.absolute_path))


def test_mock_download_writes_valid_geojson(local_storage):
    from save_earth.handlers.shared.save_earth_utils import alpr

    res = alpr.download(use_mock=True, force=True)
    assert res.used_mock and not res.was_cached
    assert res.feature_count == 4
    fc = _load(res)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 4
    for f in fc["features"]:
        assert f["geometry"]["type"] == "Point"
        lon, lat = f["geometry"]["coordinates"]
        assert -180 <= lon <= 180 and -90 <= lat <= 90
        p = f["properties"]
        # every OSM tag kept verbatim + the ALPR tag convention
        assert p["man_made"] == "surveillance"
        assert p["surveillance:type"] == "ALPR"
        # derived provenance + vendor fields
        assert p["osm_url"].startswith("https://www.openstreetmap.org/node/")
        assert p["camera_vendor"] in {"flock", "motorola", "other"}


def test_vendor_classification(local_storage):
    from save_earth.handlers.shared.save_earth_utils import alpr

    fc = _load(alpr.download(use_mock=True, force=True))
    vendors = sorted(f["properties"]["camera_vendor"] for f in fc["features"])
    # mock set: 2 Flock, 1 Motorola/Vigilant, 1 untagged → other
    assert vendors == ["flock", "flock", "motorola", "other"]


def test_classify_vendor_unit():
    from save_earth.handlers.shared.save_earth_utils import alpr

    assert alpr._classify_vendor({"manufacturer": "Flock Safety"}) == "flock"
    assert alpr._classify_vendor({"brand": "Vigilant"}) == "motorola"
    assert alpr._classify_vendor({"manufacturer": "Motorola Solutions"}) == "motorola"
    assert alpr._classify_vendor({}) == "other"


def test_cache_short_circuits(local_storage):
    from save_earth.handlers.shared.save_earth_utils import alpr

    first = alpr.download(use_mock=True, force=True)
    assert not first.was_cached
    second = alpr.download(use_mock=True)  # within freshness window → cache hit
    assert second.was_cached
    assert second.feature_count == first.feature_count


def test_handler_dispatch(local_storage):
    from save_earth.handlers.sources import source_handlers

    out = source_handlers.handle({"_facet_name": "save_earth.sources.DownloadALPRCameras",
                                  "use_mock": True, "force": True})
    assert out["cache_type"] == "alpr"
    assert out["feature_count"] == 4
    assert out["used_mock"] is True


# ---------------------------------------------------------------------------
# Local-planet fallback
#
# Overpass answers this selective query in seconds because it is indexed; the
# local extracts have no index, so the same question is a full scan. Local is
# therefore the FALLBACK — it exists so that "all four mirrors throttled" stops
# being the end of the run. It is deliberately NOT the primary: the self-hosted
# split carries a replication timestamp weeks in the past and the update phase
# was never built, so promoting it would make this map's data older, on a
# registry that is actively being mapped.
# ---------------------------------------------------------------------------

_PBF = "/Volumes/afl_data_local/osm-selfhost/www/central-america-latest.osm.pbf"


def _has_extract() -> bool:
    import shutil
    from pathlib import Path
    return shutil.which("osmium") is not None and Path(_PBF).exists()


needs_extract = pytest.mark.skipif(
    not _has_extract(), reason="no osmium or no local extract")


def test_local_pbf_path_prefers_the_explicit_override(tmp_path, monkeypatch):
    from save_earth.handlers.shared.save_earth_utils import alpr

    pbf = tmp_path / "somewhere.osm.pbf"
    pbf.write_bytes(b"")
    monkeypatch.setenv("FW_ALPR_LOCAL_PBF", str(pbf))
    assert alpr.local_pbf_path() == pbf


def test_local_pbf_path_is_none_when_nothing_is_configured(monkeypatch):
    """No extract must mean the ORIGINAL Overpass error survives — a fallback
    that cannot run should not replace the message explaining the failure."""
    from save_earth.handlers.shared.save_earth_utils import alpr

    monkeypatch.setenv("FW_ALPR_LOCAL_PBF", "")
    monkeypatch.setenv("FW_OSM_LOCAL_EXTRACTS", "")
    assert alpr.local_pbf_path() is None


def test_a_continent_extract_is_not_mistaken_for_the_planet(tmp_path, monkeypatch):
    """Searching the roots is planet-ONLY on purpose. A continent extract would
    silently turn a worldwide map into a regional one, which reads as "ALPRs
    only exist here" rather than as a missing file."""
    from save_earth.handlers.shared.save_earth_utils import alpr

    (tmp_path / "north-america-latest.osm.pbf").write_bytes(b"")
    monkeypatch.setenv("FW_ALPR_LOCAL_PBF", "")
    monkeypatch.setenv("FW_OSM_LOCAL_EXTRACTS", str(tmp_path))
    assert alpr.local_pbf_path() is None

    (tmp_path / "planet-latest.osm.pbf").write_bytes(b"")
    assert alpr.local_pbf_path() == tmp_path / "planet-latest.osm.pbf"


@needs_extract
def test_local_scan_matches_the_overpass_feature_contract():
    """Both sources go through the SAME _to_feature, so this pins that the local
    path feeds it correctly — above all the OSM id.

    `--add-unique-id=type_id` puts the id at the FEATURE's top level, not in
    `properties`. The first cut read `properties["@id"]`, which silently gave
    every feature osm_id="" and an osm_url of ".../node/" that 404s — a dead
    provenance link on all 336k cameras, invisible unless you click one.
    """
    from pathlib import Path

    from save_earth.handlers.shared.save_earth_utils import alpr

    features, source = alpr._fetch_local(Path(_PBF))
    assert features, "central-america has known ALPR nodes"
    assert source.startswith("local://"), source
    assert "@" in source, "provenance must record WHICH snapshot answered"

    for f in features:
        p = f["properties"]
        assert f["geometry"]["type"] == "Point"
        assert p["osm_id"], "every feature needs its OSM id"
        assert p["osm_url"].endswith(str(p["osm_id"]))
        assert p["osm_type"] == "node"
        assert p["camera_vendor"] in ("flock", "motorola", "other")
        assert p["surveillance:type"] == "ALPR", "verbatim tags are kept"
