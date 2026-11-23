"""freshness.py -- how far each published store is behind its sources.

For every dataset under the index roots (roots.txt), compare the source
files the published store references with the aad-filelist listing corpus
as it stands now, and write one JSON document of computed values. Declared
facts (update model, upstream cadence, who holds the bytes) are in
raad.virtualize products/*/datasets.tsv and build parameters (hold-back,
corpus source) in its dataset configs; neither is repeated here.

Per dataset:
  index       interval end of the published store (its stac/collection.json),
              created and generator (flat/arrays.json meta), and whether it
              has a build record (sources.meta.tsv); catalog_interval_end
              where the group's datasets.json states another end (a store
              republished without its group catalog)
  sources     number of source files the store references
              (flat/sources.parquet), and the corpus sources they are listed
              in (found by URL, after the store's own "public" remap rules;
              corpus_source is the one listing most of them)
  stale       referenced files the corpus lists with a different size, or
              ETag where both sides have one (changed), and files under
              the URL prefix the corpus source covers that it no longer
              lists (removed): their byte references are wrong. Files
              outside every corpus source are counted as unlisted, not
              removed (BRAN2020 before 2010, which the corpus does not list)
  upstream    corpus files of the same source and the same name pattern
              (digits as wildcards) whose name date is later than the
              latest the store references (newer), and the latest of those
              dates: what a rebuild would add, hold-back included. None for
              a store whose file names carry no date

Inputs are public: data.source.coop and the aad-filelist corpus release.
No GDAL. Requires pyarrow.

  python freshness/freshness.py --out freshness.json [--roots freshness/roots.txt]
"""
import argparse
import collections
import datetime
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

import pyarrow.parquet as pq

CORPUS_API = "https://api.github.com/repos/mdsumner/aad-filelist/releases/tags/corpus"
CORPUS = "https://github.com/mdsumner/aad-filelist/releases/download/corpus"
UA = {"User-Agent": "bowerbird-byterefs freshness.py"}
DIGITS = re.compile(r"\d+")


def get(url, required=True):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and not required:
            return None
        raise


def exists(url):
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60):
            return True
    except urllib.error.HTTPError:
        return False


def roots(path):
    out = []
    for ln in open(path):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.endswith("/groups.tsv"):
            base = ln.rsplit("/", 1)[0]
            rows = [r.split("\t") for r in get(ln).decode().splitlines() if r and not r.startswith("#")]
            out += [f"{base}/{r[0]}" for r in rows[1:]]
        else:
            out.append(ln.rstrip("/"))
    return out


def norm(url):
    """A URL as the corpus and the store both spell it: no query string."""
    return url.split("?", 1)[0]


def public(source, rules):
    """The store's source as a public URL: its first matching "public" rule
    (byterefs decision 0004), else /vsicurl/ dropped."""
    for r in rules:
        if r.get("target") == "public" and source.startswith(r["prefix"]):
            return r["replacement"] + source[len(r["prefix"]):]
    return source[len("/vsicurl/"):] if source.startswith("/vsicurl/") else source


def shape(url):
    """The name pattern of a file: its directory and name with every run of
    digits as #, so the daily files of one product match each other and not
    the monthly ones or another hemisphere."""
    return re.sub(r"\d+", "#", url)


def name_date(url):
    """The latest date in a file name, as YYYYMMDD or YYYYMM: a run of 8 or
    more digits that starts with a valid date (20261102090000), or a run of
    6 (YYYYMM). A year and month split by a separator (ocean_temp_2026_07)
    is joined."""
    nm = url.rsplit("/", 1)[-1]
    best = None
    runs = DIGITS.findall(re.sub(r"((?:18|19|20)\d{2})[_-](\d{2})(?!\d)", r"\1\2", nm))
    for r in runs:
        v = None
        if len(r) >= 8 and r[:2] in ("18", "19", "20") and 1 <= int(r[4:6]) <= 12 and 1 <= int(r[6:8]) <= 31:
            v = r[:8]
        elif len(r) == 6 and r[:2] in ("18", "19", "20") and 1 <= int(r[4:6]) <= 12:
            v = r
        if v and (best is None or v > best):
            best = v
    return best


def load_corpus():
    rel = json.loads(get(CORPUS_API))
    by_url, runs = {}, None
    for a in rel["assets"]:
        nm = a["name"]
        if nm == "runs.parquet":
            runs = pq.read_table(io.BytesIO(get(a["browser_download_url"]))).to_pylist()
        if not (nm.startswith("current__") and nm.endswith(".parquet")):
            continue
        sid = nm[len("current__"):-len(".parquet")]
        t = pq.read_table(io.BytesIO(get(a["browser_download_url"])), columns=["url", "size", "etag"])
        for r in t.to_pylist():
            by_url[norm(r["url"])] = (sid, r["size"], r["etag"])
    last = max((r["run_id"] for r in runs or [] if r.get("ok")), default=None)
    return by_url, last


def dataset_report(base, ds, corpus, by_source):
    cat_end = (ds.get("interval") or [None, None])[-1]
    rep = {"base": base, "index": {"interval_end": cat_end, "created": ds.get("created"),
                                   "generator": ds.get("generator"),
                                   "build_record": exists(f"{base}/sources.meta.tsv")}}
    raw = get(f"{base}/flat/sources.parquet", required=False)
    arrays = get(f"{base}/flat/arrays.json", required=False)
    if raw is None or arrays is None:
        rep["error"] = "no flat/sources.parquet or flat/arrays.json"
        return rep
    doc = json.loads(arrays)
    meta = doc.get("meta") or {}
    rep["index"].update({k: meta[k] for k in ("created", "generator") if meta.get(k)})
    coll = get(f"{base}/stac/collection.json", required=False)
    if coll:
        iv = ((json.loads(coll).get("extent") or {}).get("temporal") or {}).get("interval") or [[None, None]]
        if iv[0][-1]:
            rep["index"]["interval_end"] = iv[0][-1]
            if cat_end and cat_end[:10] != iv[0][-1][:10]:
                rep["index"]["catalog_interval_end"] = cat_end
    rules = doc.get("remap") or []
    srcs = pq.read_table(io.BytesIO(raw)).to_pylist()
    urls = {norm(public(s["source"], rules)): s for s in srcs}
    sids = collections.Counter(corpus[u][0] for u in urls if u in corpus)
    rep["sources"] = {"n": len(urls), "listed": sum(sids.values()),
                      "corpus_source": sids.most_common(1)[0][0] if sids else None,
                      "corpus_sources": sorted(sids)}
    # the URL prefix each corpus source covers, cut back to a directory
    covers = [os.path.commonprefix(by_source[sid]).rsplit("/", 1)[0] + "/" for sid in sids]
    changed, removed, unlisted = [], [], 0
    for u, s in urls.items():
        if u not in corpus:
            if any(u.startswith(c) for c in covers):
                removed.append(u)
            else:
                unlisted += 1
            continue
        _, size, etag = corpus[u]
        if s.get("etag") and etag and s["etag"].strip('"') != etag.strip('"'):
            changed.append(u)
        elif s.get("size") is not None and size is not None and int(s["size"]) != int(size):
            changed.append(u)
    rep["sources"]["unlisted"] = unlisted
    rep["stale"] = {"changed": len(changed), "removed": len(removed),
                    "examples": sorted(changed)[:5] + sorted(removed)[:5]} if sids else None
    have = [d for d in map(name_date, urls) if d]
    if sids and have:
        # every source the store reads: Bluelink reads BRAN2020 before 2010
        # and BRAN2023 from 2010, and new months arrive in the second
        shapes, last = {shape(u) for u in urls}, max(have)
        new = [d for sid in sids for u in by_source[sid] if u not in urls and shape(u) in shapes
               for d in [name_date(u)] if d and d > last]
        rep["upstream"] = {"indexed_to": last, "newer": len(new), "newest_date": max(new) if new else last}
    else:
        rep["upstream"] = None
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--roots", default=os.path.join(os.path.dirname(__file__), "roots.txt"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    corpus, run = load_corpus()
    by_source = collections.defaultdict(list)
    for u, (sid, _, _) in corpus.items():
        by_source[sid].append(u)
    out = {"generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "corpus_run": run, "datasets": {}}
    for root in roots(a.roots):
        prod = json.loads(get(f"{root}/product.json"))
        for ds in json.loads(get(f"{root}/datasets.json")):
            if not ds.get("published"):
                continue
            base = f"{prod.get('public_base', root).rstrip('/')}/{ds['dataset']}"
            print(f"freshness: {ds['dataset']}", file=sys.stderr)
            r = dataset_report(base, ds, corpus, by_source)
            r["product"] = prod.get("id")
            out["datasets"][ds["dataset"]] = r
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"freshness: {len(out['datasets'])} datasets, corpus run {run} -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
