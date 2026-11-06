# Layout proposal (not adopted)

Status: proposal for review, 2026-11-06. Nothing is collected yet; collect/collect.py implements option B for trying it out. The
first dataset to land is `access-esm1-6.historical.ocean-3d-temp-1mon-mean`
(raad.virtualize `config/access-esm1-6.sh`, corpus source
`pawsey_ct_esm1_6`). This file replaces the tree in README.md once agreed.

## Choices to make

### C1. Where the table bytes live

| option | what | for | against |
|---|---|---|---|
| A. all in git | every build's Parquet committed | one place, `git clone` is the whole archive | grows forever; a BRAN 3D variable is hundreds of MB of refs; git is poor at binary churn |
| B. small text in git, tables as release assets (recommended) | git: `arrays.json`, `build.json`, index tables; one GitHub release per build holds `refs_*.parquet`, `sources.parquet`, `sources.meta.tsv`, `plan.vrt` | matches aad-filelist (decision D2); the meta that people review is diffable; 2 GB per asset | two places; release assets are not range-readable from every client |
| C. object store only | git holds code and the index; tables on source.coop or Pawsey | range reads, scale | needs a production credential (source.coop) or a Pawsey bucket owner; nothing can land until then |

Recommended: B now, and the same tree mirrored to an object store (C)
when a credential exists, as aad-filelist plans for the corpus.

### C2. Build identity

`build_id = <created, UTC, %Y%m%dT%H%M%SZ>-<generator commit, 7>`, e.g.
`20261106T013000Z-1a2b3c4`, taken from `arrays.json` `meta` (`created`,
`generator`). A `-dirty` generator is refused: a collected build must name
a commit. Release tag: `<dataset>@<build_id>`.

### C3. What is collected per build

| file | collected | where (option B) |
|---|---|---|
| `flat/arrays.json` | yes | git |
| `build.json` (written by the collector: files, sizes, sha256, release tag, counts) | yes | git |
| `flat/refs_<array>.parquet` | yes | release |
| `flat/sources.parquet` | yes | release |
| `sources.meta.tsv` | yes; required (no build record, no stale check) | release |
| `plan.vrt` | yes (the plan and verify oracle) | release |
| `store.sqlite` | no: derivable from the flat table (3 GB per bluelink 3D variable) | not kept |
| rendered forms (`mdim.vrt`, `kerchunk/`, `icechunk/`, `stac/`, `grist/`) | no: they belong to the product | not kept |

### C4. Index tables

- `index/stores.parquet`: one row per (dataset, build_id): `dataset,
  build_id, product, release_tag, format_version, generator, created,
  config, source_ids, n_sources, n_refs, default_target, current`.
- `index/sources.parquet`: the reverse index, one row per (source_id, url,
  dataset, build_id), from `sources.meta.tsv`. A corpus `change` or
  `remove` on a url finds every build that references it.
- `<dataset>/current.json`: the build_id a product publishes. A product
  that has no source.coop home yet (ACCESS-ESM1.6) still has a current
  build here.

### C5. Scope

README.md says "for the ausantarctic products". ACCESS-ESM1.6 is a
separate ACCESS-NRI product, so the scope is any dataset harvested by
raad.virtualize, with `product` a column (empty until a product exists).

## Tree under option B

```
README.md  LAYOUT.md
index/
  stores.parquet
  sources.parquet
<dataset>/
  current.json
  <build_id>/
    arrays.json
    build.json
collect/                      the collector (Python + pyarrow, no GDAL)
```

Release `<dataset>@<build_id>`: `refs_<array>.parquet`, `sources.parquet`,
`sources.meta.tsv`, `plan.vrt`.

## Collecting one build

```
python collect/collect.py <raad.virtualize work dir> [--product <id>]
```

Reads `<work>/out/flat/` and `<work>/{plan.vrt,sources.meta.tsv}`, checks
the generator is clean, writes `<dataset>/<build_id>/`, appends the index
rows, creates the release and uploads the four files. The stale check
(raad.virtualize `py/stale.py`) then runs over every current build's
`sources.meta.tsv` after each corpus run.
