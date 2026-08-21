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


# ---------------------------------------------------------------------------
# Source selection: prefer what is MAINTAINED
# ---------------------------------------------------------------------------


def _continents(root, missing=()):
    root.mkdir(parents=True, exist_ok=True)
    from save_earth.handlers.shared.save_earth_utils import alpr
    for c in alpr.LOCAL_CONTINENTS:
        if c in missing:
            continue
        (root / f"{c}-latest.osm.pbf").write_bytes(b"")
    return root


def test_the_maintained_continent_set_beats_the_frozen_planet(tmp_path, monkeypatch):
    """Phase 2 of the planet split keeps the CONTINENT extracts current via
    replication and updates the single planet file never. On this deployment
    that was a 40-day gap: planet 2026-07-12, extracts 2026-08-20. Reading the
    planet would have made the fallback far staler than it needed to be."""
    from save_earth.handlers.shared.save_earth_utils import alpr

    www = _continents(tmp_path / "www")
    planet_root = tmp_path / "extracts"
    planet_root.mkdir()
    (planet_root / "planet-latest.osm.pbf").write_bytes(b"")

    monkeypatch.delenv("FW_ALPR_LOCAL_PBF", raising=False)
    monkeypatch.setenv("FW_OSM_SELFHOST_WWW", str(www))
    monkeypatch.setenv("FW_OSM_LOCAL_EXTRACTS", str(planet_root))

    srcs = alpr.local_sources()
    assert len(srcs) == len(alpr.LOCAL_CONTINENTS)
    assert all("planet" not in p.name for p in srcs)


def test_a_partial_continent_set_is_refused(tmp_path, monkeypatch):
    """A partial set would make a WORLDWIDE map look regional — "ALPRs only
    exist in North America" — rather than looking like a missing file. The
    planet-only guard did not disappear when continents were added; it moved."""
    from save_earth.handlers.shared.save_earth_utils import alpr

    www = _continents(tmp_path / "www", missing=("africa", "oceania"))
    monkeypatch.delenv("FW_ALPR_LOCAL_PBF", raising=False)
    monkeypatch.setenv("FW_OSM_SELFHOST_WWW", str(www))
    monkeypatch.setenv("FW_OSM_LOCAL_EXTRACTS", "")
    assert alpr.local_sources() == []


def test_merging_extracts_deduplicates_boundary_features(monkeypatch):
    """Regional extracts are cut with a buffer past their polygon, so a camera
    near a seam really is in two of them. Concatenating would double-count
    exactly the boundary features — invisible in a total, obvious on the map."""
    from pathlib import Path

    from save_earth.handlers.shared.save_earth_utils import alpr

    def _fake(pbf, **k):
        shared = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
                  "properties": {"osm_id": "111"}}
        own = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 1]},
               "properties": {"osm_id": pbf.name}}
        return [shared, own], f"local://{pbf.name}@2026-08-20T00:00:00Z"

    monkeypatch.setattr(alpr, "_fetch_local", _fake)
    feats, source = alpr._fetch_local_many([Path("a.pbf"), Path("b.pbf")])
    ids = [f["properties"]["osm_id"] for f in feats]
    assert ids.count("111") == 1, "the shared boundary feature must appear once"
    assert len(feats) == 3
    assert source.startswith("local://2-extracts@")


def test_merged_provenance_reports_the_oldest_input(monkeypatch):
    """A merged set is only as current as its stalest member."""
    from pathlib import Path

    from save_earth.handlers.shared.save_earth_utils import alpr

    stamps = {"new.pbf": "2026-08-20T00:00:00Z", "old.pbf": "2026-07-12T00:00:00Z"}

    def _fake(pbf, **k):
        return ([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
                  "properties": {"osm_id": pbf.name}}],
                f"local://{pbf.name}@{stamps[pbf.name]}")

    monkeypatch.setattr(alpr, "_fetch_local", _fake)
    _feats, source = alpr._fetch_local_many([Path("new.pbf"), Path("old.pbf")])
    assert source.endswith("@2026-07-12T00:00:00Z"), source
