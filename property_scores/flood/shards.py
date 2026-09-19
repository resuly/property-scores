"""Manifest + SQL plumbing for the per-source feature shards.

WHY THIS EXISTS. Until 2026-09-19 every feature source lived in ONE
/data/features/features.duckdb (31 GiB, 16.4M rows, 150 sources). Refreshing a
724-row council flood layer shadow-copied all 31 GiB and rebuilt the RTREE over
all 16.4M rows, because both the copy and the index were sized by the FILE, not
by the source. That cost produced three incidents: site-wide 502s on 2026-07-16,
six-minute timeouts on 2026-08-10, and on 2026-09-19 a 25-minute machine-wide
livelock that only an OCI hard reboot cleared (33 GB of copy write-back filled
the 200 GB volume's queue while the RTREE build squeezed the page cache to
0.5 GB). Capping IO and memory around it only made the same work slower.

So the storage unit is now the REFRESH unit: one DuckDB file per source, each
with its own features table, its own RTREE, and one manifest listing them all.
`build.py --only nsw_bv_map` now rewrites a 908 MB file instead of a 31 GiB one,
and the 19 other sources on the 1-to-3-day cadence are all under 350 MB with
most under 1 MB.

DELIBERATELY DEPENDENCY-FREE. /var/www/property-scores is a separate repo with
its own gunicorn on :8099 that opens the same features database directly and
does not import da_leads. This module is stdlib + duckdb only so that repo can
copy the file verbatim. Do not import anything from da_leads here.

Layout under the manifest's directory (production /data/features, which is a
symlink to the 200 GB ext4 volume /data/tiles/features -- no reflink, so a
"cheap" copy is a real copy):

    manifest.json          the index: one entry per source
    shards/<source>.duckdb one DuckDB file per source

Manifest entries carry rows and bytes per shard on purpose: with 150 files, a
source that silently ingested 0 rows or tripled in size has to be visible from
one `cat manifest.json`, not from 150 stat calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from typing import Any, Iterable, Optional

# The manifest is the switch. When it is absent, both build.py and lookup.py
# fall back to the single-file features.duckdb, which is what makes the
# production rollout reversible: deploy the code, run the migration, point
# FEATURES_MANIFEST at the result, and move the file aside to go back.
DEFAULT_MANIFEST = os.environ.get("FEATURES_MANIFEST",
                                  "/data/features/manifest.json")

MANIFEST_VERSION = 1
SHARDS_DIRNAME = "shards"

# Source names come from the build registry (tiles/refresh/manifest.py and
# build.py's _EXTRA_SOURCES), never from a request. They are still validated
# here because a name becomes both a FILENAME and a SQL IDENTIFIER, and those
# are two different ways for a stray character to stop being data.
_SAFE_NAME = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{0,95}\Z")


class ManifestError(ValueError):
    """The manifest on disk is not something we are willing to read."""


def is_safe_source_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name or ""))


def alias_for(source: str) -> str:
    """DuckDB catalog alias for a shard. Prefixed so it cannot collide with
    `main`, `temp`, `system` or a column name."""
    if not is_safe_source_name(source):
        raise ManifestError(f"unsafe source name for a SQL alias: {source!r}")
    return "sh_" + source


def sql_str_list(values) -> str:
    """SQL literal list for an IN (...) over trusted internal identifiers.

    Values are category/state/source names from the registry and the
    manifest, never request input, but they are quoted anyway so a typo'd
    name cannot terminate the string.
    """
    return ", ".join("'" + str(v).replace("'", "''") + "'"
                     for v in sorted(values))


def shard_relpath(source: str) -> str:
    if not is_safe_source_name(source):
        raise ManifestError(f"unsafe source name for a filename: {source!r}")
    return f"{SHARDS_DIRNAME}/{source}.duckdb"


# ---------------------------------------------------------------------------
# Manifest IO
# ---------------------------------------------------------------------------

def manifest_dir(manifest_path: str) -> str:
    return os.path.dirname(os.path.abspath(manifest_path))


def shard_path(manifest_path: str, entry: dict) -> str:
    """Absolute path of a shard file. Entries store the path RELATIVE to the
    manifest so the whole directory can be moved, copied to a staging box or
    restored under a different mount without a rewrite."""
    return os.path.join(manifest_dir(manifest_path), entry["file"])


def load_manifest(manifest_path: str = DEFAULT_MANIFEST) -> Optional[dict]:
    """Parse the manifest, or None when there is no shard layout to read.

    None means "fall back to the single file", so a MISSING manifest is a
    legitimate answer. A manifest that exists but does not parse is not: that
    is a corrupt index, and silently serving the frozen legacy file instead
    would hide it for as long as nobody compares row counts.
    """
    try:
        with open(manifest_path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ManifestError(f"cannot read {manifest_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ManifestError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise ManifestError(f"{manifest_path} has no sources object")
    return data


def write_manifest(manifest_path: str, manifest: dict) -> None:
    """Replace the manifest atomically.

    The temp file is created in the manifest's OWN directory, never in /tmp:
    /data/features is a symlink onto a separate ext4 volume and os.replace
    across devices raises. A reader that stats the manifest between our write
    and our replace sees the old one, which is a complete index of shards that
    all still exist, so there is no window where the index is half-written.
    """
    directory = manifest_dir(manifest_path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".manifest.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(manifest, fh, indent=1, sort_keys=True, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, manifest_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "build_id": "", "sources": {}}


def compute_build_id(manifest: dict) -> str:
    """One token that changes whenever ANY shard changes.

    The single-file design got this free: every bake renamed a new inode over
    the top, so inode+mtime identified the build. Sharded, a refresh moves one
    file out of 150, so the identity has to be a digest over all of them. It is
    what web/cache_provenance.py stamps cached answers with, so it must change
    on every ingest and must NOT change when nothing did (a spuriously moving
    id would reject the whole enrichment cache on every request).
    """
    parts = []
    for name in sorted(manifest.get("sources", {})):
        entry = manifest["sources"][name]
        parts.append("{}\t{}\t{}\t{}".format(
            name, entry.get("build_id", ""), entry.get("rows", ""),
            entry.get("bytes", "")))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def shard_build_id(path: str) -> str:
    """inode:mtime of one shard file, the same token the single-file design
    used for the whole database. A shard is only ever replaced by rename, so a
    new inode is exactly a new build of that source."""
    st = os.stat(path)
    return f"{st.st_ino}:{int(st.st_mtime)}"


def manifest_stamp(manifest_path: str = DEFAULT_MANIFEST) -> str:
    """Cheap "has the index moved" token: ONE stat, no parse, no query.

    This is what a long-lived reader polls. It has to be cheap because it runs
    on a timer in every gunicorn worker, and it has to cover content because
    build.py replaces the manifest by rename (new inode) on every ingest.
    Returns "" when the manifest is absent, which the caller reads as "there is
    no shard layout", not as "nothing changed".
    """
    try:
        st = os.stat(manifest_path)
    except OSError:
        return ""
    return f"{st.st_ino}:{int(st.st_mtime)}:{st.st_size}"


# ---------------------------------------------------------------------------
# Shard selection
# ---------------------------------------------------------------------------

def select_sources(
    manifest: dict,
    *,
    categories: Optional[Iterable[str]] = None,
    states: Optional[Iterable[str]] = None,
    sources: Optional[Iterable[str]] = None,
) -> list[str]:
    """Source names whose shard can possibly answer a query, sorted.

    This is the whole read-side win: picking shards from the manifest replaces
    the `category IN (...)` / `state = ?` predicates that had to be left OUT of
    the SQL, because adding them defeated the RTREE index scan and turned a
    point lookup into a sequential scan over every geometry (see the long note
    in lookup.lookup_features_at). Scoping by file instead of by predicate
    keeps the single spatial predicate per shard AND stops reading the other
    140 files at all.

    `states` always admits the national 'au' layers, exactly as the Python-side
    filters in lookup.py do: LGA, IBRA and the ABS boundaries belong with every
    state's answer.
    """
    want_cats = ({str(c).lower().strip() for c in categories}
                 if categories is not None else None)
    want_states = ({str(s).lower().strip() for s in states} | {"au"}
                   if states is not None else None)
    want_sources = set(sources) if sources is not None else None
    out = []
    for name, entry in manifest.get("sources", {}).items():
        if want_sources is not None and name not in want_sources:
            continue
        if want_cats is not None and str(entry.get("category", "")).lower() not in want_cats:
            continue
        if want_states is not None and str(entry.get("state", "")).lower() not in want_states:
            continue
        out.append(name)
    return sorted(out)


def attach_statements(manifest_path: str, manifest: dict,
                      names: Optional[Iterable[str]] = None) -> list[tuple[str, str]]:
    """[(source, "ATTACH '<file>' AS <alias> (READ_ONLY)")] for the named shards.

    READ_ONLY is not a nicety: a reader that attached read-write would take
    DuckDB's single-writer lock on the shard and the next refresh of that
    source could not swap it in.
    """
    chosen = sorted(manifest.get("sources", {})) if names is None else sorted(names)
    out = []
    for name in chosen:
        entry = manifest["sources"][name]
        path = shard_path(manifest_path, entry).replace("'", "''")
        out.append((name, f"ATTACH '{path}' AS {alias_for(name)} (READ_ONLY)"))
    return out


def union_sql(cols: str, where: str, names: Iterable[str]) -> str:
    """UNION ALL of one `SELECT cols FROM <shard>.features WHERE where` per shard.

    Written out per shard rather than hidden behind a VIEW over the same
    union, which is the obvious way to keep every existing `FROM features`
    statement working and was measured and rejected. On DuckDB 1.5.1 with 150
    toy shards (2026-09-19) a view pushes the filter no further than the
    union: the plan is 150 SEQ_SCANs and every geometry is materialised. The
    explicit form plans 150 RTREE_INDEX_SCANs. On the toy data that was 50 ms
    against 7 ms; on production it is the difference between an index lookup
    and reading 14.7 GB of geometry.

    For scale, not as a win: an unscoped point query across all 150 shards
    takes ~7 ms against ~0.2 ms on the single file. Fanning out costs
    something, and that is the accepted price. Most queries scope to a
    category or a state and touch a handful of shards; the refresh cost this
    buys back is measured in whole minutes of machine-wide stall.
    """
    chosen = sorted(names)
    if not chosen:
        return ""
    where_sql = f" WHERE {where}" if where else ""
    return " UNION ALL ".join(
        f"SELECT {cols} FROM {alias_for(n)}.features{where_sql}" for n in chosen)


def wrap(body: str, *, order_by: Optional[str] = None,
         limit: Optional[str] = None) -> str:
    """Put ORDER BY / LIMIT around a union body.

    A union of N branches cannot carry them per branch and stay correct: each
    branch would order and truncate its own rows. Both clauses reference the
    union's output columns, so the caller must project whatever they name.
    """
    sql = f"SELECT * FROM ({body})" if (order_by or limit) else body
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {limit}"
    return sql


def repeat_params(params: Optional[list], n: int) -> list:
    """One branch's parameters, repeated once per branch, in branch order.

    A union is N copies of the same statement, so a caller that passes bound
    parameters has to pass N copies of them. Getting this wrong does not raise
    a type error, it silently binds branch 2's point to branch 1's radius, so
    it lives here rather than at each call site.
    """
    if not params:
        return []
    return list(params) * n


# ---------------------------------------------------------------------------
# A minimal reader, for consumers outside da_leads
# ---------------------------------------------------------------------------

class ShardReader:
    """Read-only DuckDB over the shards, reopened when the manifest moves.

    da_leads' own read path (tiles/services/feature_lookup/lookup.py) does not
    use this: it has its own connection, query lock and cache invalidation
    wired into the request path. This class exists for the OTHER reader --
    /var/www/property-scores on :8099, a separate repo with its own gunicorn
    that opens the features database directly and does not import da_leads. It
    can copy this file and use this class unchanged.

    Not thread-safe by itself: a DuckDBPyConnection is not safe for concurrent
    execute() from several threads (it corrupts the heap, not just the result),
    so `lock` is exposed and `query` takes it.
    """

    def __init__(self, manifest_path: str = DEFAULT_MANIFEST,
                 *, memory_limit: str = "512MB", threads: str = "1"):
        import threading
        self.manifest_path = manifest_path
        self.memory_limit = memory_limit
        self.threads = threads
        self.lock = threading.Lock()
        self._conn = None
        self._manifest: Optional[dict] = None
        self._stamp = ""

    def manifest(self) -> Optional[dict]:
        self._refresh()
        return self._manifest

    def build_id(self) -> str:
        self._refresh()
        return (self._manifest or {}).get("build_id", "")

    def _refresh(self) -> None:
        stamp = manifest_stamp(self.manifest_path)
        if self._conn is not None and stamp == self._stamp:
            return
        import duckdb
        manifest = load_manifest(self.manifest_path)
        if manifest is None:
            self.close()
            self._manifest, self._stamp = None, ""
            return
        conn = duckdb.connect(":memory:", config={
            "memory_limit": self.memory_limit, "threads": self.threads})
        conn.execute("LOAD spatial")
        for _name, stmt in attach_statements(self.manifest_path, manifest):
            conn.execute(stmt)
        self.close()
        self._conn, self._manifest, self._stamp = conn, manifest, stamp

    def query(self, cols: str, where: str, params: Optional[list] = None, *,
              categories: Optional[Iterable[str]] = None,
              states: Optional[Iterable[str]] = None,
              sources: Optional[Iterable[str]] = None,
              order_by: Optional[str] = None,
              limit: Optional[str] = None) -> list[tuple]:
        with self.lock:
            self._refresh()
            if self._conn is None or self._manifest is None:
                raise FileNotFoundError(
                    f"no feature shard manifest at {self.manifest_path}")
            names = select_sources(self._manifest, categories=categories,
                                   states=states, sources=sources)
            if not names:
                return []
            sql = wrap(union_sql(cols, where, names),
                       order_by=order_by, limit=limit)
            return self._conn.execute(
                sql, repeat_params(params, len(names))).fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def summarise(manifest: dict) -> dict[str, Any]:
    """Totals for an operator or a test: shard count, rows, bytes."""
    sources = manifest.get("sources", {})
    return {
        "shards": len(sources),
        "rows": sum(int(e.get("rows") or 0) for e in sources.values()),
        "bytes": sum(int(e.get("bytes") or 0) for e in sources.values()),
    }
