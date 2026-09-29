#!/usr/bin/env python3
"""graph_store.py — indexed SQLite backend for the memory graph (DEFAULT), with
JSON fallback + portability export + invisible one-time auto-migration.

WHY: dinomem-neuron users never migrate manually, so the default backend must
scale by itself. JSON keeps every win (single cp-portable file, stdlib, git-
diffable, inspectable) EXCEPT indexed access — every query pays an O(n) full
parse (measured: 0.71s load / 1.84s e2e on a 16MB / 12297-node / 34836-edge
live graph). SQLite keeps all the JSON wins (single graph.db file, stdlib =
zero-dep/zero-setup, sqlite3-inspectable, cp-portable) AND is indexed, so the
per-query parse tax disappears.

DESIGN CONTRACT (must not break downstream):
  * NODE IDENTITY == original list index. graph_search BFS uses the int list
    position as the node id, and relation/sim edges reference nodes by that int.
    We persist it as `nodes.idx` PRIMARY KEY and always return nodes() ordered
    by idx so list-position semantics survive unchanged.
  * relation_edges: a/b are int idx OR -1 (entity-name edges keyed by
    subject/object strings). Full row is preserved via payload_json so the graph
    round-trips byte-for-byte through replace_all()->meta()/edges().
  * sim edges kept in full; hot-path columns (a,b,neural_score) are indexed.
  * meta() returns the non-hot-path round-trip payload (entity_nodes,
    entity_edges, forward_refs, counts, generated, params) so a rebuild can
    re-emit an identical memory_graph.json for portability / back-compat.

BACKENDS:
  * SqliteBackend (default): graph.db beside memory_graph.json.
  * JsonBackend (fallback): raw memory_graph.json (used only if sqlite3 is
    somehow unavailable, or when the caller forces it).
  * open(ws) auto-selects SQLite, auto-migrating from memory_graph.json on first
    run if graph.db is absent but the JSON exists. Invisible to callers.

PUBLIC API (both backends satisfy the same surface):
  store = open(workspace_dir)          # or open(json_path=..., db_path=...)
  store.nodes()            -> list[dict]   (ordered by idx; index-stable)
  store.relation_edges()   -> list[dict]
  store.edges()            -> list[dict]   (similarity edges)
  store.meta()             -> dict         (counts + entity_* + forward_refs + generated + params)
  store.neighbors(idx)     -> list[dict]   (relation+sim edges touching idx; convenience)
  store.replace_all(payload)               (full rebuild; dual-writes JSON for portability)
  store.close()
"""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

# ---- canonical file locations (relative to workspace) -----------------------
GRAPH_SUBDIR = os.path.join("kb", "memory_neuron", "l2_graph")
JSON_NAME = "memory_graph.json"
DB_NAME = "graph.db"

SCHEMA_VERSION = 1


# =============================================================================
# helpers
# =============================================================================
def _paths(workspace: Optional[str], json_path: Optional[str], db_path: Optional[str]):
    """Resolve (json_path, db_path) from a workspace dir or explicit overrides."""
    if json_path or db_path:
        jp = json_path or (os.path.join(os.path.dirname(db_path), JSON_NAME) if db_path else None)
        dp = db_path or (os.path.join(os.path.dirname(json_path), DB_NAME) if json_path else None)
        return jp, dp
    if not workspace:
        raise ValueError("graph_store.open needs workspace or json_path/db_path")
    base = os.path.join(workspace, GRAPH_SUBDIR)
    return os.path.join(base, JSON_NAME), os.path.join(base, DB_NAME)


def _load_json(path: str) -> Dict[str, Any]:
    with builtins.open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _edge_valid_at(edge: Dict[str, Any], as_of: str) -> bool:
    """Temporal filter (identical semantics to graph_search._edge_valid_at):
    valid_at <= as_of < invalid_at, missing bounds open, untimed edges fail-open.
    Ported here so the store is self-contained (no import back into graph_search)."""
    va = edge.get("valid_at")
    iv = edge.get("invalid_at")
    if not va and not iv:
        return True
    if va and as_of < va:
        return False
    if iv and as_of >= iv:
        return False
    return True


# =============================================================================
# SQLite backend (DEFAULT)
# =============================================================================
class SqliteBackend:
    """Indexed backend. graph.db beside memory_graph.json."""

    def __init__(self, db_path: str, json_path: str):
        self.db_path = db_path
        self.json_path = json_path
        # noob/install safety: parent dir may not exist yet on a fresh install
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # WHY busy_timeout: on big graphs (200M-400M+ code graphs) a rebuild writer
        # and a concurrent code_query reader collide. sqlite's DEFAULT busy handler
        # gives up IMMEDIATELY ("database is locked") instead of waiting, which the
        # caller then surfaces as a generic timeout/failure. Waiting is almost always
        # correct here: the competing txn is short (a read) or is the rebuild itself.
        # Override via DINOMEM_SQLITE_TIMEOUT_S for pathological boxes.
        _timeout_s = float(os.environ.get("DINOMEM_SQLITE_TIMEOUT_S", "60"))
        self.conn = sqlite3.connect(db_path, timeout=_timeout_s)
        self.conn.row_factory = sqlite3.Row
        # Belt-and-suspenders: connect(timeout=) covers the Python-side busy handler,
        # PRAGMA busy_timeout covers statements issued on this connection.
        self.conn.execute(f"PRAGMA busy_timeout = {int(_timeout_s * 1000)}")
        # WAL lets readers (code_query) proceed while a rebuild writes, instead of
        # blocking on the single write lock. fail-soft: some filesystems (NFS, some
        # overlayfs) reject WAL -- a rollback-journal DB still works, just less concurrent.
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
        self._ensure_schema()

    # ---- schema -------------------------------------------------------------
    def _ensure_schema(self):
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                idx     INTEGER PRIMARY KEY,   -- == original list index (stable id)
                node_id TEXT,
                file    TEXT,
                degree  INTEGER,
                title   TEXT,
                snippet TEXT,
                payload TEXT                   -- full node dict (round-trip)
            );
            CREATE TABLE IF NOT EXISTS relation_edges (
                rid        INTEGER PRIMARY KEY AUTOINCREMENT,
                a          INTEGER,            -- src idx or -1
                b          INTEGER,            -- tgt idx or -1
                label      TEXT,
                subject    TEXT,
                object     TEXT,
                confidence REAL,
                scope      TEXT,
                valid_at   TEXT,
                invalid_at TEXT,
                source     TEXT,
                payload    TEXT                -- full edge dict (round-trip)
            );
            CREATE TABLE IF NOT EXISTS sim_edges (
                rid          INTEGER PRIMARY KEY AUTOINCREMENT,
                a            INTEGER,
                b            INTEGER,
                neural_score REAL,
                payload      TEXT              -- full edge dict (round-trip)
            );
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT                         -- JSON blob (round-trip payload)
            );
            CREATE INDEX IF NOT EXISTS ix_rel_a ON relation_edges(a);
            CREATE INDEX IF NOT EXISTS ix_rel_b ON relation_edges(b);
            CREATE INDEX IF NOT EXISTS ix_rel_label ON relation_edges(label);
            CREATE INDEX IF NOT EXISTS ix_sim_a ON sim_edges(a);
            CREATE INDEX IF NOT EXISTS ix_sim_b ON sim_edges(b);
            CREATE INDEX IF NOT EXISTS ix_sim_ns ON sim_edges(neural_score);
            """
        )
        c.execute(
            "INSERT OR IGNORE INTO meta(k,v) VALUES('__schema_version__', ?)",
            (json.dumps(SCHEMA_VERSION),),
        )
        c.commit()

    def is_populated(self) -> bool:
        cur = self.conn.execute("SELECT COUNT(*) FROM nodes")
        return cur.fetchone()[0] > 0

    # ---- migration ----------------------------------------------------------
    def migrate_from_json(self, payload: Optional[Dict[str, Any]] = None):
        """One-time (or forced) import of memory_graph.json -> graph.db."""
        if payload is None:
            payload = _load_json(self.json_path)
        self.replace_all(payload, dual_write_json=False)

    # ---- write path ---------------------------------------------------------
    def replace_all(self, payload: Dict[str, Any], dual_write_json: bool = True):
        """Full rewrite. Build is batch, so a full replace is fine; the WIN is on
        the READ path via indexed load. Dual-writes memory_graph.json for
        portability / back-compat unless disabled (e.g. during migration)."""
        c = self.conn
        c.execute("DELETE FROM nodes")
        c.execute("DELETE FROM relation_edges")
        c.execute("DELETE FROM sim_edges")
        c.execute("DELETE FROM meta WHERE k != '__schema_version__'")

        nodes = payload.get("nodes", []) or []
        node_rows = []
        for i, n in enumerate(nodes):
            idx = n.get("idx", i)  # trust explicit idx; fall back to position
            node_rows.append((
                idx, n.get("id"), n.get("file"), n.get("degree"),
                n.get("title"), n.get("snippet"),
                json.dumps(n, ensure_ascii=False),
            ))
        c.executemany(
            "INSERT OR REPLACE INTO nodes(idx,node_id,file,degree,title,snippet,payload)"
            " VALUES(?,?,?,?,?,?,?)", node_rows,
        )

        rel_rows = []
        for e in payload.get("relation_edges", []) or []:
            rel_rows.append((
                e.get("a", -1), e.get("b", -1), e.get("label"),
                e.get("subject"), e.get("object"), e.get("confidence"),
                e.get("scope"), e.get("valid_at"), e.get("invalid_at"),
                e.get("source"), json.dumps(e, ensure_ascii=False),
            ))
        c.executemany(
            "INSERT INTO relation_edges(a,b,label,subject,object,confidence,"
            "scope,valid_at,invalid_at,source,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            rel_rows,
        )

        sim_rows = []
        for e in payload.get("edges", []) or []:
            ns = e.get("neural_score", e.get("sim", e.get("weight", e.get("similarity"))))
            sim_rows.append((
                e.get("a"), e.get("b"), ns, json.dumps(e, ensure_ascii=False),
            ))
        c.executemany(
            "INSERT INTO sim_edges(a,b,neural_score,payload) VALUES(?,?,?,?)",
            sim_rows,
        )

        # meta = everything that is NOT the hot-path node/edge lists
        meta_payload = {k: v for k, v in payload.items()
                        if k not in ("nodes", "edges", "relation_edges")}
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('__graph_meta__', ?)",
                  (json.dumps(meta_payload, ensure_ascii=False),))
        c.commit()

        if dual_write_json:
            tmp = self.json_path + ".tmp"
            with builtins.open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.json_path)

    # ---- read path ----------------------------------------------------------
    def nodes(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute("SELECT payload FROM nodes ORDER BY idx ASC")
        return [json.loads(r[0]) for r in cur.fetchall()]

    def relation_edges(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute("SELECT payload FROM relation_edges ORDER BY rid ASC")
        return [json.loads(r[0]) for r in cur.fetchall()]

    def edges(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute("SELECT payload FROM sim_edges ORDER BY rid ASC")
        return [json.loads(r[0]) for r in cur.fetchall()]

    def meta(self) -> Dict[str, Any]:
        cur = self.conn.execute("SELECT v FROM meta WHERE k='__graph_meta__'")
        row = cur.fetchone()
        return json.loads(row[0]) if row else {}

    def neighbors(self, idx: int) -> List[Dict[str, Any]]:
        """Relation + sim edges touching idx (indexed lookup — the payoff)."""
        out = []
        cur = self.conn.execute(
            "SELECT payload FROM relation_edges WHERE a=? OR b=?", (idx, idx))
        out.extend(json.loads(r[0]) for r in cur.fetchall())
        cur = self.conn.execute(
            "SELECT payload FROM sim_edges WHERE a=? OR b=?", (idx, idx))
        out.extend(json.loads(r[0]) for r in cur.fetchall())
        return out

    def neighbors_filtered(self, idx, min_confidence, scope_allow=None,
                           as_of=None, min_sim=0.80):
        """Indexed BFS expansion of ONE node. Returns [{target,label,confidence,
        edge_type}] applying EXACTLY build_adjacency's filters, but only for edges
        touching idx (indexed WHERE — never scans the full edge list).

        relation edges: directed a->b, so only a=idx counts as outgoing. Skips
        -1 endpoints, confidence>=min_confidence, scope membership (default
        'owner'), temporal validity via as_of.
        sim edges: undirected, neural_score>=min_sim, either endpoint == idx."""
        out = []
        # --- relation edges: directed, idx must be the SOURCE (a) ---
        cur = self.conn.execute(
            "SELECT payload FROM relation_edges WHERE a=? AND b>=0 AND confidence>=? "
            "ORDER BY rid ASC",
            (idx, min_confidence))
        for (pl,) in cur.fetchall():
            e = json.loads(pl)
            if scope_allow is not None and e.get("scope", "owner") not in scope_allow:
                continue
            if as_of is not None and not _edge_valid_at(e, as_of):
                continue
            out.append({"target": e.get("b"), "label": e.get("label", "related_to"),
                        "confidence": e.get("confidence", 0.0), "edge_type": "relation"})
        # --- sim edges: undirected, idx on either side, neural_score>=min_sim ---
        cur = self.conn.execute(
            "SELECT a,b,neural_score FROM sim_edges WHERE (a=? OR b=?) AND neural_score>=? "
            "ORDER BY rid ASC",
            (idx, idx, min_sim))
        for a, b, ns in cur.fetchall():
            other = b if a == idx else a
            out.append({"target": other, "label": "similar_to",
                        "confidence": ns, "edge_type": "similarity"})
        return out

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


# =============================================================================
# JSON backend (FALLBACK / portability)
# =============================================================================
class JsonBackend:
    """Raw memory_graph.json. Used only when sqlite3 is unavailable or forced.
    Same accessor surface as SqliteBackend (no indexing win — this is the
    compatibility floor, not the default)."""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self._data = _load_json(json_path) if os.path.exists(json_path) else {}

    def is_populated(self) -> bool:
        return bool(self._data.get("nodes"))

    def migrate_from_json(self, payload: Optional[Dict[str, Any]] = None):
        if payload is not None:
            self._data = payload

    def replace_all(self, payload: Dict[str, Any], dual_write_json: bool = True):
        self._data = payload
        if dual_write_json:
            tmp = self.json_path + ".tmp"
            with builtins.open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.json_path)

    def nodes(self) -> List[Dict[str, Any]]:
        return self._data.get("nodes", []) or []

    def relation_edges(self) -> List[Dict[str, Any]]:
        return self._data.get("relation_edges", []) or []

    def edges(self) -> List[Dict[str, Any]]:
        return self._data.get("edges", []) or []

    def meta(self) -> Dict[str, Any]:
        return {k: v for k, v in self._data.items()
                if k not in ("nodes", "edges", "relation_edges")}

    def neighbors(self, idx: int) -> List[Dict[str, Any]]:
        out = [e for e in self.relation_edges() if e.get("a") == idx or e.get("b") == idx]
        out += [e for e in self.edges() if e.get("a") == idx or e.get("b") == idx]
        return out

    def neighbors_filtered(self, idx, min_confidence, scope_allow=None,
                           as_of=None, min_sim=0.80):
        """Same filters as SqliteBackend.neighbors_filtered, list-scan (no index)."""
        out = []
        for e in self.relation_edges():
            if e.get("a") != idx:
                continue
            b = e.get("b")
            if b is None or b < 0:
                continue
            if e.get("confidence", 0.0) < min_confidence:
                continue
            if scope_allow is not None and e.get("scope", "owner") not in scope_allow:
                continue
            if as_of is not None and not _edge_valid_at(e, as_of):
                continue
            out.append({"target": b, "label": e.get("label", "related_to"),
                        "confidence": e.get("confidence", 0.0), "edge_type": "relation"})
        for e in self.edges():
            a, b = e.get("a"), e.get("b")
            if a is None or b is None:
                continue
            if idx not in (a, b):
                continue
            ns = e.get("neural_score", e.get("sim", e.get("weight", e.get("similarity", 0.0))))
            if ns < min_sim:
                continue
            out.append({"target": b if a == idx else a, "label": "similar_to",
                        "confidence": ns, "edge_type": "similarity"})
        return out

    def close(self):
        pass


# =============================================================================
# open() — auto-selects SQLite, invisible one-time migration
# =============================================================================
def open(workspace: Optional[str] = None, *, json_path: Optional[str] = None,
         db_path: Optional[str] = None, backend: str = "auto"):
    """Return a graph store. Default: SQLite, auto-migrating from
    memory_graph.json on first run if graph.db is absent but the JSON exists.

    backend: "auto" (SQLite w/ JSON fallback), "sqlite", or "json"."""
    jp, dp = _paths(workspace, json_path, db_path)

    if backend == "json":
        return JsonBackend(jp)

    if backend in ("auto", "sqlite"):
        try:
            import sqlite3 as _s  # noqa: F401
        except Exception:
            if backend == "sqlite":
                raise
            return JsonBackend(jp)

        store = SqliteBackend(dp, jp)
        # invisible one-time migration: db empty but JSON present -> import
        if not store.is_populated() and jp and os.path.exists(jp):
            store.migrate_from_json()
        return store

    raise ValueError(f"unknown backend {backend!r}")


# =============================================================================
# CLI smoke: python3 graph_store.py <workspace> [--rebuild-db]
# =============================================================================
if __name__ == "__main__":
    import sys

    # Workspace resolution (priority): explicit argv[1] > DINOMEM_WORKSPACE env >
    # install-time sed placeholder > this file's grandparent dir. Matches the
    # repo-wide resolution pattern (see memory_graph.py) so no hardcoded live path.
    _ws_default = "DINOMEM_WORKSPACE_PLACEHOLDER"
    if _ws_default.startswith("DINOMEM_"):  # sed did not run (dev / raw checkout)
        _ws_default = os.environ.get("DINOMEM_WORKSPACE",
                                     str(Path(__file__).resolve().parent.parent))
    ws = sys.argv[1] if len(sys.argv) > 1 else _ws_default
    force_rebuild = "--rebuild-db" in sys.argv

    jp, dp = _paths(ws, None, None)
    if force_rebuild and os.path.exists(dp):
        os.remove(dp)
        print(f"[smoke] removed existing {dp}")

    t0 = time.time()
    st = open(ws)
    t_open = time.time() - t0

    t1 = time.time()
    ns = st.nodes()
    t_nodes = time.time() - t1

    t2 = time.time()
    re = st.relation_edges()
    se = st.edges()
    t_edges = time.time() - t2

    m = st.meta()
    print(f"[smoke] backend={type(st).__name__}")
    print(f"[smoke] open()={t_open:.3f}s  nodes()={t_nodes:.3f}s ({len(ns)})  "
          f"edges()={t_edges:.3f}s (rel={len(re)} sim={len(se)})")
    print(f"[smoke] meta keys={sorted(m.keys())}")
    print(f"[smoke] counts: node={m.get('node_count')} edge={m.get('edge_count')} "
          f"rel={m.get('relation_edge_count')} entity_nodes={len(m.get('entity_nodes',[]))}")
    # index identity check
    if ns:
        bad = [i for i, n in enumerate(ns[:2000]) if n.get("idx", i) != i]
        print(f"[smoke] node idx==list-position mismatches (first 2000): {len(bad)}")
    # indexed neighbor lookup timing
    if ns:
        t3 = time.time()
        nb = st.neighbors(0)
        print(f"[smoke] neighbors(0)={time.time()-t3:.4f}s ({len(nb)} edges)")
