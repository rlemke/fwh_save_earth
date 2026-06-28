"""Offline tests for the ethnic/cultural enclave source + map layers.

Fully offline: the classifier is pure (name -> heritage), and the cache path uses
``use_mock=True`` against a temp local storage backend — no network, no MongoDB.
The key regression is the word-boundary anchoring of the short Korean/Japanese
patterns ("Yorktown"/"Blacktown"/"Cooktown" must NOT read as Koreatowns).
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def local_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "local")
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))
    yield tmp_path


def test_classification_basic():
    from save_earth.handlers.shared.save_earth_utils import enclaves

    cases = {
        "Chinatown": "chinese",
        "China Town": "chinese",
        "Japantown": "japanese",
        "Little Tokyo": "japanese",
        "Koreatown": "korean",
        "K-Town Historic District": "korean",
        "Little Italy": "italian",
        "Little India": "south-asian",
        "Little Saigon": "vietnamese",
        "Greektown": "greek",
        "Little Havana": "cuban",
        "Thaitown": "thai",
        "Little Portugal": "portuguese",
        "Corktown": "irish",
        "Little Ireland": "irish",
        "Jewish Quarter": "jewish",
    }
    for name, slug in cases.items():
        h = enclaves._classify(name)
        assert h is not None and h.slug == slug, f"{name!r} -> {h and h.slug!r}, want {slug!r}"


def test_korean_word_boundary_regression():
    """The short ``k-?town`` pattern must be word-anchored so common -ktown place
    names are NOT misread as Koreatowns (this flooded korean 87->15 in dev)."""
    from save_earth.handlers.shared.save_earth_utils import enclaves

    for false_positive in [
        "Yorktown", "Blacktown", "Cooktown", "Bucktown",
        "Bricktown", "Parktown", "Darktown", "Birmingham at Yorktown",
    ]:
        assert enclaves._classify(false_positive) is None, f"{false_positive!r} wrongly classified"
    # "Corktown" is genuinely Irish (Detroit/Toronto) — it MUST classify, not be dropped.
    assert enclaves._classify("Corktown").slug == "irish"


def test_no_unrelated_name_matches():
    from save_earth.handlers.shared.save_earth_utils import enclaves

    for unrelated in ["Downtown", "Old Town", "Newtown", "Georgetown", "Charlottetown"]:
        assert enclaves._classify(unrelated) is None


def test_heritages_well_formed():
    from save_earth.handlers.shared.save_earth_utils import enclaves

    slugs = [h.slug for h in enclaves.HERITAGES]
    assert len(slugs) == len(set(slugs)), "heritage slugs must be unique"
    for h in enclaves.HERITAGES:
        assert h.color.startswith("#") and h.label and h.pattern


def test_mock_download_writes_per_heritage(local_storage):
    from save_earth.handlers.shared.save_earth_utils import enclaves

    res = enclaves.download(use_mock=True, force=True)
    assert res.used_mock
    assert res.feature_count >= 1
    assert res.heritage_count >= 1
    # every reported heritage slug is a known one with a positive count
    known = {h.slug for h in enclaves.HERITAGES}
    for slug, n in res.per_heritage.items():
        assert slug in known and n >= 1
