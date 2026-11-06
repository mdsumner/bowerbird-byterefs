"""collect.py -- add one raad.virtualize build to the central byte-ref collection.

Layout (LAYOUT.md, option B): small text in git, tables as one GitHub
release per build.

  git      <dataset>/<build_id>/arrays.json   byterefs/0.1 meta, as built
           <dataset>/<build_id>/build.json    files, sizes, sha256, counts
           <dataset>/current.json             the build a product publishes
           index/stores.parquet               one row per (dataset, build_id)
           index/sources.parquet              source_id, url, dataset, build_id
  release  <dataset>@<build_id>: refs_<array>.parquet, sources.parquet,
           sources.meta.tsv, plan.vrt

The input is a raad.virtualize work directory after the render step:
<work>/out/flat/{arrays.json, refs_*.parquet, sources.parquet},
<work>/plan.vrt and <work>/sources.meta.tsv (the build record written by
py/corpus.py --meta; a build without one cannot be stale-checked and is
refused).

  build_id   <created, UTC, %Y%m%dT%H%M%SZ>-<generator commit, 7 hex>,
             from arrays.json meta.created and meta.generator; a "-dirty"
             or unversioned generator is refused
  --product  product id for stores.parquet (empty until a product exists)
  --repo     the bowerbird-byterefs checkout (default: this script's repo)
  --upload   gh: create the release and upload with the gh CLI;
             none: copy the release files to <repo>/release/<tag>/
             (git-ignored) and print the gh command (default: gh when gh is
             on PATH, else none)
  --not-current
             record the build without making it the current one

Git is not touched: review, commit and push the changes afterwards.

Usage:
  python collect/collect.py /path/to/raad.virtualize/work/<dataset>
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STORES_SCHEMA = pa.schema([
    ("dataset", pa.string()), ("build_id", pa.string()), ("product", pa.string()),
    ("release_tag", pa.string()), ("format_version", pa.string()),
    ("generator", pa.string()), ("created", pa.string()), ("config", pa.string()),
    ("source_ids", pa.list_(pa.string())), ("n_sources", pa.int64()),
    ("n_refs", pa.int64()), ("n_chunks", pa.int64()), ("bytes", pa.int64()),
    ("current", pa.bool_()), ("collected", pa.string()),
])
SOURCES_SCHEMA = pa.schema([
    ("source_id", pa.string()), ("url", pa.string()),
    ("dataset", pa.string()), ("build_id", pa.string()),
])


def die(msg):
    sys.exit(f"collect: {msg}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build_id_of(meta):
    created = meta.get("created") or die("arrays.json meta has no created")
    # R format(Sys.time(), tz = "UTC", usetz = TRUE): "2026-11-06 06:59:00 UTC"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", created)
    if not m:
        die(f"cannot read meta.created {created!r}")
    stamp = "%s%s%sT%s%s%sZ" % m.groups()
    gen = (meta.get("generator") or "").strip()
    rev = gen.split()[-1] if gen else ""
    if not rev or rev == "unversioned" or rev.endswith("-dirty"):
        die(f"generator {gen!r} does not name a clean commit; rebuild from a committed checkout")
    # git describe --always: a bare hash, or <tag>-<n>-g<hash>
    h = re.search(r"(?:-g)?([0-9a-f]{7,40})$", rev)
    if not h:
        die(f"cannot find a commit hash in generator {gen!r}")
    return f"{stamp}-{h.group(1)[:7]}", stamp


def read_meta_tsv(path):
    t = pacsv.read_csv(path, parse_options=pacsv.ParseOptions(delimiter="\t"),
                       convert_options=pacsv.ConvertOptions(
                           column_types={"source_id": pa.string(), "url": pa.string()}))
    for c in ("source_id", "url"):
        if c not in t.column_names:
            die(f"{path} has no {c} column")
    return t


def append(path, schema, new, replace):
    """Append rows to a Parquet index, dropping old rows where replace(row) is true."""
    rows = []
    if os.path.exists(path):
        rows = [r for r in pq.read_table(path).to_pylist() if not replace(r)]
    rows += new
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("work")
    ap.add_argument("--product", default="")
    ap.add_argument("--repo", default=HERE)
    ap.add_argument("--upload", choices=["gh", "none"],
                    default="gh" if shutil.which("gh") else "none")
    ap.add_argument("--not-current", action="store_true")
    a = ap.parse_args()

    flat = os.path.join(a.work, "out", "flat")
    aj = os.path.join(flat, "arrays.json")
    if not os.path.exists(aj):
        die(f"no {aj}: run the render step first")
    doc = json.load(open(aj))
    meta = doc.get("meta") or {}
    dataset = meta.get("dataset") or die("arrays.json meta has no dataset")
    build_id, _ = build_id_of(meta)
    tag = f"{dataset}@{build_id}"

    release = sorted(f for f in os.listdir(flat) if re.match(r"^refs_.+\.parquet$", f))
    if not release:
        die(f"no refs_*.parquet in {flat}")
    paths = {f: os.path.join(flat, f) for f in release + ["sources.parquet"]}
    paths["sources.meta.tsv"] = os.path.join(a.work, "sources.meta.tsv")
    paths["plan.vrt"] = os.path.join(a.work, "plan.vrt")
    for f, p in paths.items():
        if not os.path.exists(p):
            hint = " (the build record: build from the corpus list, py/corpus.py --meta)" \
                if f == "sources.meta.tsv" else ""
            die(f"missing {p}{hint}")

    rec = read_meta_tsv(paths["sources.meta.tsv"])
    n_sources = pq.read_metadata(paths["sources.parquet"]).num_rows
    if rec.num_rows != n_sources:
        die(f"sources.meta.tsv has {rec.num_rows} rows, sources.parquet {n_sources}: "
            "the build record is not from this build")
    source_ids = sorted(set(rec.column("source_id").to_pylist()))
    summ = [x.get("summary") or {} for x in doc.get("arrays", []) if x.get("storage") == "referenced"]
    n_refs = sum(int(s.get("present", 0)) for s in summ)
    n_chunks = sum(int(s.get("chunks", 0)) for s in summ)
    nbytes = sum(int(s.get("bytes", 0)) for s in summ)

    bdir = os.path.join(a.repo, dataset, build_id)
    if os.path.exists(os.path.join(bdir, "build.json")):
        die(f"{dataset}/{build_id} is already collected")
    os.makedirs(bdir, exist_ok=True)
    shutil.copyfile(aj, os.path.join(bdir, "arrays.json"))
    files = [{"name": f, "size": os.path.getsize(p), "sha256": sha256(p)} for f, p in paths.items()]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    build = {
        "dataset": dataset, "build_id": build_id, "release_tag": tag,
        "product": a.product or None, "format_version": doc.get("format_version"),
        "generator": meta.get("generator"), "created": meta.get("created"),
        "config": meta.get("config"), "source_ids": source_ids,
        "n_sources": n_sources, "n_refs": n_refs, "n_chunks": n_chunks, "bytes": nbytes,
        "remap": doc.get("remap", []), "files": files, "collected": now,
    }
    with open(os.path.join(bdir, "build.json"), "w") as f:
        json.dump(build, f, indent=2)
        f.write("\n")

    current = not a.not_current
    idx = os.path.join(a.repo, "index")
    if current and os.path.exists(os.path.join(idx, "stores.parquet")):
        t = pq.read_table(os.path.join(idx, "stores.parquet")).to_pylist()
        for r in t:
            if r["dataset"] == dataset:
                r["current"] = False
        pq.write_table(pa.Table.from_pylist(t, schema=STORES_SCHEMA), os.path.join(idx, "stores.parquet"))
    row = {k: build.get(k) for k in STORES_SCHEMA.names if k in build}
    row.update(product=a.product, current=current, config=build["config"] or "")
    n_st = append(os.path.join(idx, "stores.parquet"), STORES_SCHEMA, [row],
                  lambda r: r["dataset"] == dataset and r["build_id"] == build_id)
    srows = [{"source_id": s, "url": u, "dataset": dataset, "build_id": build_id}
             for s, u in zip(rec.column("source_id").to_pylist(), rec.column("url").to_pylist())]
    n_sr = append(os.path.join(idx, "sources.parquet"), SOURCES_SCHEMA, srows,
                  lambda r: r["dataset"] == dataset and r["build_id"] == build_id)
    if current:
        with open(os.path.join(a.repo, dataset, "current.json"), "w") as f:
            json.dump({"dataset": dataset, "build_id": build_id, "release_tag": tag}, f, indent=2)
            f.write("\n")

    print(f"collect: {dataset} build {build_id}: {n_refs} refs, {n_sources} sources "
          f"({', '.join(source_ids)}); index {n_st} builds, {n_sr} source rows")
    notes = (f"byterefs tables for {dataset}, build {build_id} "
             f"({n_refs} refs over {n_sources} sources). Metadata: {dataset}/{build_id}/ in git.")
    cmd = ["gh", "release", "create", tag, "--repo", "mdsumner/bowerbird-byterefs",
           "--title", tag, "--notes", notes, "--latest=false"]
    if a.upload == "gh":
        subprocess.run(cmd + list(paths.values()), check=True)
        print(f"collect: release {tag} created")
    else:
        stage = os.path.join(a.repo, "release", tag)
        os.makedirs(stage, exist_ok=True)
        for f, p in paths.items():
            shutil.copyfile(p, os.path.join(stage, f))
        print(f"collect: release files in {stage}; upload with:")
        print("  " + " ".join(cmd[:4] + ["--repo", cmd[5], "--title", tag, "--notes", repr(notes),
                                         "--latest=false", os.path.join(stage, "*")]))
    print("collect: then review, commit and push the git changes")


if __name__ == "__main__":
    main()
