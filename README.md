# bowerbird-byterefs

Central collation of harvested byte references for the ausantarctic
products, and the scheduled check that they still point at the right bytes.

One of two repos around one engine:

| repo | answers | runs |
|---|---|---|
| [mdsumner/aad-filelist](https://github.com/mdsumner/aad-filelist) | what files exist at each source, and what changed (listing corpus) | light, scheduled, GitHub Actions |
| mdsumner/bowerbird-byterefs (this repo) | what is inside those files: chunk offsets, sizes, codecs, per store build | collates builds; the scheduled stale check |
| [mdsumner/raad.virtualize](https://github.com/mdsumner/raad.virtualize) | the engine: plan, harvest, collate, render, verify, publish | where the heavy work runs (VM, Gadi, Setonix) |

The shared key is the corpus `source_id`. Format: byterefs/0.1
([byterefs/spec](https://github.com/byterefs/spec)). Status: empty
scaffold; nothing is built or published from here yet.

## What it collects

Each product build (raad.virtualize `run.sh`) writes a byterefs/0.1 table
and a build record. This repo gathers them into one place, keyed by
dataset and build:

```
<refs-product>/
  stores.parquet                    one row per (dataset, build_id)
  sources.parquet                   source_id, url, dataset, build_id: reverse index
  <dataset>/<build_id>/
    arrays.json                     byterefs/0.1 meta + remap rules (spec decision 0004)
    refs_<array>.parquet
    sources.parquet
    sources.meta.tsv                build record (raad.virtualize py/corpus.py --meta)
    plan.vrt                        the plan and verify oracle
  <dataset>/current.json            build_id of the published build
```

- `stores.parquet` (suggested columns): `dataset, build_id, product,
  publish_prefix, format_version, generator, created, source_ids,
  n_sources, n_refs, default_target, stale_checked, stale_n`.
- `build_id`: generator commit + created time, as `arrays.json` `meta`
  already carries them. Append-only.
- Keyed by dataset, not source_id: one dataset can read several sources and
  one source feeds several datasets (G02202 north and south both read
  `cdr_sic_G02202_V6`). `sources.parquet` is the reverse index a corpus
  change event needs to find the stores it invalidates.
- The refs product location is not chosen yet (a source.coop product such
  as `ausantarctic/byterefs`, needing a production write credential).

## The stale check

After each corpus run, compare every published store's `sources.meta.tsv`
with the corpus `current__<source_id>.parquet` (raad.virtualize
`py/stale.py`: `removed` or `changed` by etag, then size). A stale store is
flagged in `stores.parquet`; rebuilding it is the product repo's job.

## Open

- Parquet fragments plus a manifest, or Lance, for the versioned table
  (asked of Dewey Dunnington).
- Whether products keep their own copy of `flat/` and `store.sqlite` or
  point at this one.
- No published store has a `sources.meta.tsv` yet: each dataset needs one
  rebuild on the corpus before it can be collected.

Background: the production handover map (per-product repos, scratch vs
production nesting) in the project files, `production/per-product-repo-map.md`.

## Icechunk and kerchunk from a collected build

A collected build is a byterefs/0.1 table; to get an Icechunk repository
from it, run raad.virtualize `py/render_icechunk.py` on the build directory
(arrays.json beside refs_<array>.parquet). For Icechunk ingestion straight
from files, see Earthmover's [icechunk-datacube-ingestion agent
skill](https://github.com/earth-mover/agent-skills/tree/main/icechunk-datacube-ingestion);
its batching, manifest-splitting and memory advice applies to our renders
too. Its advice that kerchunk is deprecated does not apply here: raad.virtualize
keeps publishing kerchunk alongside the table, because GDAL and other
non-Python readers need it.
