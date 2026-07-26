# Cache, storage & packaging

**FFL:** `src/save_earth/ffl/save_earth.ffl` (schemas `SourceFetchResult`, `MapBundle`; mixin `RetryPolicy`) ·
**Tools:** `src/save_earth/tools/_save_earth_tools/{sidecar,storage}.py` ·
**Shim:** `src/save_earth/handlers/shared/save_earth_utils.py` ·
**Packaging:** `src/save_earth/__init__.py`, `src/save_earth/catalog.py` + `catalog.yaml`, `pyproject.toml` ·
**Contracts:** `agent-spec/cache-layout.agent-spec.yaml`, `agent-spec/tools-pattern.agent-spec.yaml`

## Overview

This is the cross-cutting substrate every source and the renderer share: the
**sidecar-backed cache**, the **`FW_STORAGE` backend abstraction** (local / hdfs /
s3-MinIO), the **CLI-tool ↔ handler code-sharing shim**, and the **packaging**
(`facetwork.domains` entry point + `catalog.yaml` capability manifest). It documents
"how does a download become a durable, portable cached artifact, and how do the
terminal and the runtime end up reading the same bytes?" — not a single map feature.

## How it works

**One cache, two surfaces.** The real implementation lives in
`save_earth/tools/_save_earth_tools/`. The CLI tools (`save_earth/tools/`) and the
FFL handlers both use it: handlers import it through a one-time `sys.path` shim
(`save_earth_utils.py`) that re-exports each `_save_earth_tools` module, so
`from ..shared.save_earth_utils import power, seismic, …` works without path
gymnastics. The terminal and the runtime therefore read/write the **same on-disk
cache**.

**Sidecar cache layout** (`sidecar.py`, per `cache-layout.agent-spec.yaml`): every
artifact has a sibling `<artifact>.meta.json` recording `version, namespace,
cache_type, relative_path, kind, size_bytes, sha256, generated_at`, plus optional
`source` / `tool` / `extra`. Per-entry sidecars mean N writers on N keys never
contend; only same-key overwrites take a per-entry `fcntl` lock (`entry_lock`).
Readers treat a missing sidecar as "entry absent", so the **rename-artifact-first,
write-sidecar-second** order is load-bearing. Paths derive as
`<cache_root>/<namespace>/<cache_type>/<relative_path>` — here `namespace` is always
`save-earth`.

**Write protocol.** Downloads stream to a **local** staging dir
(`local_staging_subdir`), then `storage.finalize_from_local(stage, final)` moves the
complete file into the (possibly remote) cache atomically, then the sidecar is
written. Object stores can't do partial writes, so staging is always local even when
the backend is s3.

## Fan-out

Not applicable — this is infrastructure shared by all features. (The concurrency
story it enables: per-entry sidecars + atomic finalize are what let independent
source tasks write different keys across the fleet without a shared lock.)

## Data & fields

- **`SourceFetchResult`** schema (FFL) mirrors the `_save_earth_tools` `FetchResult`
  dataclasses: `cache_type, relative_path, feature_count, size_bytes, sha256,
  source_url, was_cached, used_mock`. `_result_payload` in the source handler
  projects the dataclass into this shape (deliberately omitting `cache_type` so each
  handler sets it explicitly).
- **`MapBundle`** schema: `region_key, output_dir, html_path, layer_count,
  layer_counts: Json`.
- **`RetryPolicy(max_retries=3, backoff_ms=2000)`** mixin — `implicit default_retry`,
  applied to every network-facing download for exponential backoff.

## External libraries / binaries

- **`requests`** (pip) — used by the source tools, not this layer directly.
- **`PyYAML`** (pip) — `catalog.py` loads `catalog.yaml`.
- The **s3/hdfs** backends soft-import `facetwork.runtime.storage`
  (`S3StorageBackend` / `HDFSStorageBackend`) only when selected — the local backend
  is pure stdlib (`os`, `fcntl`, `shutil`, `subprocess`). No binary/geospatial deps.

## Facets & workflows

No facets of its own. Relevant declarations: the two schemas and the `RetryPolicy`
mixin above. Handler registration: `handlers/__init__.py` exposes
`register_all_registry_handlers` (RegistryRunner) and `register_all_handlers`
(AgentPoller); imports are deferred into function bodies to avoid import-lock
deadlocks under concurrent handler loading. `src/save_earth/__init__.py` exports
`domain = DomainPackage(name="save-earth", ffl_dir=…, register_handlers=…)` — the
`facetwork.domains` entry point (`save-earth = "save_earth:domain"` in
`pyproject.toml`) the runner discovers.

## Cache / output

- **Roots** (`storage.py`): one `FW_DATA_ROOT` with five derived subtrees —
  `cache/`, `staging/`, `tmp/`, `_indexes/`, `locks/` — each individually overridable
  (`FW_CACHE_ROOT`, `FW_STAGING_ROOT`, …). Backend defaults: local
  `/Volumes/afl_data`, hdfs `/user/afl`, s3 `s3://afl-cache`.
- **Backend selection:** `FW_STORAGE` = `local` (default) / `hdfs` / `s3`. On the
  fleet, `s3` points at the shared MinIO, so caches + map HTML are portable across
  hosts with no shared filesystem. `S3Storage.localize` maintains a local
  read-through cache for readers needing a real file handle.
- **`catalog.yaml`** is a machine-readable manifest of this package's reusable
  **workflows** (intent summaries + tags, à la `fw_catalog_match`) and **facets**
  (capability index with effect/cost, à la `fw_capabilities`); `catalog.py` loads it
  (`load_manifest` / `workflows()` / `facets()`). `tests/test_catalog_manifest.py`
  guards it.

## Gotchas & notes

- **Keep staging/scratch local even on s3/hdfs.** `FW_DATA_ROOT` may be an object
  store, which would poison the derived staging root; `local_scratch_root()` /
  `FW_LOCAL_SCRATCH` give a guaranteed-local staging dir for downloads and
  read-through caches.
- **Sidecar write order is a correctness invariant** — artifact rename first, sidecar
  second; a reader seeing the sidecar assumes the artifact is complete.
- **HDFS has no locking and buffers writes in RAM** (documented in `storage.py`);
  it's fine for these modest GeoJSON files but not a general large-file path, and
  `finalize_dir_from_local` is unimplemented on HDFS.
- **`exists_and_valid` does not re-verify sha256** (too expensive) — it checks the
  sidecar + size only.

## Related specs

- [map-rendering](map-rendering.md) — the consumer that reads every cached layer.
- All source specs — every `Download*` writes through this cache:
  [epa-and-litter](epa-and-litter.md), [seismic](seismic.md),
  [osm-overpass-sources](osm-overpass-sources.md), [enclaves](enclaves.md),
  [power-transmission](power-transmission.md), [renewable-siting](renewable-siting.md),
  [semiconductor](semiconductor.md).
