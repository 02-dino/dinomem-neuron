#!/usr/bin/env python3
"""
data_query.py — exact SQL retrieval over the neuron's structured-data store.

Reads kb/data_store/data.duckdb (materialized by procedures/data_register.py) and
runs read-only SELECT queries. This is the EXACT half of the neuron and its PRIMARY path:
filter / range (<,>,=) / sort / COUNT / SUM / GROUP BY / JOIN — the things embeddings can't do.
Two optional aids ride alongside (neither regresses the exact path):
  --suggest  : fuzzy value routing (jaro_winkler/levenshtein/ILIKE, NO embeddings) — resolve an
               inexact filter VALUE (typo/synonym/partial) to a real stored value.
  --semantic : OPTIONAL per-prose-column semantic rank — for tables with a free-text (prose) column
               (survey feedback, reviews, tickets), rank rows by MEANING and join matched rowids back
               to full structured rows. Opt-in per column; optional-dep guarded (exit 3 if the embedding
               stack is absent) so the exact-SQL path is never affected.

SAFETY (mandatory, user-facing): READ-ONLY. Only a single SELECT/WITH statement is
allowed. Any INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH/COPY/PRAGMA/INSTALL/LOAD/CREATE
(and multi-statement) is rejected before execution, and the DB is opened read_only.

CLI:
  data_query.py --sql "SELECT * FROM sales WHERE price < 50"      run a query
  data_query.py --list                                           list catalog tables + schema + keywords
  data_query.py --table sales                                    show one table's schema + keywords
  data_query.py --sql "..." --format json                        json output (default: text)
  data_query.py --suggest "united states"                         resolve an inexact filter VALUE -> real stored value
  data_query.py --suggest "Wiget" --table sales --column product  scope the fuzzy resolve to one table/column

Import API:
  from data_query import data_query, list_tables, suggest_value
  rows = data_query("SELECT COUNT(*) FROM sales")   # -> list[dict]
  tables = list_tables()                            # -> catalog dict
  hits = suggest_value("united states")             # -> [{table, column, value, score}] (fuzzy value resolver)

Fuzzy value routing (--suggest / suggest_value): queries are not assumed accurate. An inexact
filter value (typo / synonym / partial / wrong case) is resolved against the catalog's profiled
categorical sample_values via DuckDB jaro_winkler + levenshtein + ILIKE-contains (no embeddings).
It SUGGESTS the real (table, column, value); the agent then writes the exact WHERE. Never rewrites SQL.

Routing: --list/list_tables() expose each table's columns + keywords so the agent can
pick the right table (catalog.json is the intent router).
"""
import argparse
import json
import os
import re
import sys

# ── import guard (feature-unavailable, not a crash) ──────────────────────────
try:
    import duckdb
except ImportError:
    sys.stderr.write(
        "data_query: duckdb not installed — structured_data feature unavailable.\n"
        "Install with: python3 -m pip install duckdb  (semantic tools are unaffected)\n"
    )
    sys.exit(3)

HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ is a sibling of kb/ inside the neuron package
STORE_DIR = os.path.normpath(os.path.join(HERE, "..", "kb", "data_store"))
DB_PATH = os.path.join(STORE_DIR, "data.duckdb")
CATALOG_PATH = os.path.join(STORE_DIR, "catalog.json")
TEXT_INDEX_DIR = os.path.join(STORE_DIR, "text_index")

# Statements/keywords that must never run through the read-only query path.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|MERGE|"
    r"ATTACH|DETACH|COPY|PRAGMA|INSTALL|LOAD|SET|CALL|EXPORT|IMPORT|"
    r"VACUUM|ANALYZE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


class QueryError(Exception):
    pass


def _die(msg, code=1):
    sys.stderr.write("data_query: " + msg + "\n")
    sys.exit(code)


def _strip_sql(sql):
    """Remove -- line comments and /* */ block comments, collapse whitespace."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip()


def _assert_select_only(sql):
    """Raise QueryError unless sql is a single read-only SELECT/WITH statement."""
    clean = _strip_sql(sql)
    if not clean:
        raise QueryError("empty query")
    # reject multiple statements (allow a single optional trailing semicolon)
    body = clean[:-1] if clean.endswith(";") else clean
    if ";" in body:
        raise QueryError("multiple statements are not allowed (read-only single SELECT only)")
    # must begin with SELECT or WITH
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", body):
        raise QueryError("only SELECT / WITH queries are allowed (read-only)")
    # no DDL/DML/side-effecting keywords anywhere
    m = _FORBIDDEN.search(body)
    if m:
        raise QueryError(f"forbidden keyword '{m.group(1).upper()}' — this store is read-only (SELECT only)")
    return body


def _load_catalog():
    if not os.path.exists(CATALOG_PATH):
        return {"tables": {}}
    with open(CATALOG_PATH, "r") as f:
        cat = json.load(f)
    cat.setdefault("tables", {})
    return cat


def _connect_ro():
    if not os.path.exists(DB_PATH):
        _die(f"no data store yet at {DB_PATH}. Register a dataset first with procedures/data_register.py.", 2)
    return duckdb.connect(DB_PATH, read_only=True)


def data_query(sql):
    """Run a read-only SELECT; return list[dict]. Raises QueryError on unsafe SQL."""
    body = _assert_select_only(sql)
    con = _connect_ro()
    try:
        try:
            cur = con.execute(body)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        except duckdb.Error as e:
            # DuckDB parse/binder/runtime errors (bad column, reserved word, unknown
            # table, type mismatch) surface as a clean QueryError instead of a raw
            # traceback — cmd_query catches QueryError and exits 2 with the message.
            raise QueryError(f"SQL error: {str(e).splitlines()[0]}")
    finally:
        con.close()
    return [dict(zip(cols, r)) for r in rows]


def list_tables():
    """Return the catalog (tables -> schema + keywords) for routing."""
    return _load_catalog()

# ── fuzzy value routing (Dino: don't assume the query value is exact) ────────────
# Resolve an inexact filter VALUE (typo / synonym / partial / wrong case) to the real
# stored values, using the sample_values the catalog already profiled per categorical
# column. DuckDB-native fuzzy fns (jaro_winkler_similarity + levenshtein) + ILIKE-contains;
# no embeddings. This is a SUGGESTER: it returns candidate (table, column, value) matches;
# the agent then writes a correct exact WHERE. It never rewrites/executes SQL itself.
def _fuzzy_score(query, value):
    """0..1 similarity between a user value and a stored value, pure-python fallback-free
    numbers are computed in DuckDB in suggest_value(); this is only a light local prefilter."""
    import difflib
    q, v = str(query).lower().strip(), str(value).lower().strip()
    if not q or not v:
        return 0.0
    if q == v:
        return 1.0
    contains = 1.0 if (q in v or v in q) else 0.0
    ratio = difflib.SequenceMatcher(None, q, v).ratio()
    return max(contains * 0.95, ratio)

def _initials(s):
    words = [w for w in re.split(r"[^A-Za-z0-9]+", str(s).lower()) if w]
    return "".join(w[0] for w in words)

def _acronym_match(a, b):
    """True if one side is the initialism of the other (e.g. 'united states' <-> 'us',
    'uk' <-> 'united kingdom'). Case/punct-insensitive. Requires 2+ words on the long side."""
    a1, b1 = str(a).lower().strip(), str(b).lower().strip()
    a_compact = re.sub(r"[^a-z0-9]", "", a1)
    b_compact = re.sub(r"[^a-z0-9]", "", b1)
    a_words = [w for w in re.split(r"[^a-z0-9]+", a1) if w]
    b_words = [w for w in re.split(r"[^a-z0-9]+", b1) if w]
    if len(a_words) >= 2 and _initials(a1) == b_compact and len(b_compact) >= 2:
        return True
    if len(b_words) >= 2 and _initials(b1) == a_compact and len(a_compact) >= 2:
        return True
    return False

def suggest_value(text, table=None, column=None, limit=5, min_score=0.55):
    """Resolve inexact `text` against catalog categorical sample_values.
    Returns list of {table, column, value, score} sorted best-first.
    Uses DuckDB jaro_winkler_similarity + levenshtein for scoring (falls back to a
    local ratio if duckdb scoring errors). Read-only, catalog-only — no table scan."""
    cat = _load_catalog().get("tables", {})
    # gather candidate (table, column, value) triples from profiled categorical dims
    cands = []
    for tname, e in cat.items():
        if table and tname != table:
            continue
        for c in e.get("columns", []):
            if column and c["name"] != column:
                continue
            if not c.get("categorical"):
                continue
            for v in c.get("sample_values") or []:
                if v is None:
                    continue
                cands.append((tname, c["name"], v))
    if not cands:
        return []
    q = str(text).strip()
    scored = []
    # score in DuckDB for good jaro_winkler; one in-memory connection, no store touch.
    # PLUS an acronym/initialism boost so "united states" resolves to "US" (string-edit
    # distance can't bridge that; initials can). Handles both directions (query is the
    # long form or the acronym).
    try:
        con = duckdb.connect()
        for tname, cname, v in cands:
            vs = str(v)
            row = con.execute(
                "SELECT greatest("
                "  jaro_winkler_similarity(lower(?), lower(?)),"
                "  CASE WHEN lower(?) LIKE '%'||lower(?)||'%' OR lower(?) LIKE '%'||lower(?)||'%' THEN 0.95 ELSE 0 END,"
                "  1.0 - (levenshtein(lower(?), lower(?))::DOUBLE / greatest(length(?), length(?), 1))"
                ")",
                [q, vs, vs, q, q, vs, q, vs, q, vs],
            ).fetchone()
            s = float(row[0])
            if _acronym_match(q, vs):
                s = max(s, 0.9)
            scored.append((tname, cname, v, s))
        con.close()
    except Exception:
        scored = []
        for t, c, v in cands:
            s = _fuzzy_score(q, v)
            if _acronym_match(q, str(v)):
                s = max(s, 0.9)
            scored.append((t, c, v, s))
    scored.sort(key=lambda x: x[3], reverse=True)
    out = [{"table": t, "column": c, "value": v, "score": round(s, 3)}
           for (t, c, v, s) in scored if s >= min_score]
    return out[:limit]

def cmd_suggest(text, table, column, fmt):
    hits = suggest_value(text, table=table, column=column)
    if fmt == "json":
        print(json.dumps(hits, indent=2, default=_json_default))
        return
    if not hits:
        print(f"no close value match for '{text}'"
              + (f" in {table}" if table else "")
              + (f".{column}" if column else "")
              + ". (Only profiled categorical dimensions are searched; use --list to see them.)")
        return
    best = hits[0]
    print(f"did you mean {best['table']}.{best['column']} = '{best['value']}' ?  (score {best['score']})")
    print(f"  → e.g.  SELECT * FROM {best['table']} WHERE \"{best['column']}\" = '{best['value']}'")
    if len(hits) > 1:
        print("other candidates:")
        for h in hits[1:]:
            print(f"    {h['table']}.{h['column']} = '{h['value']}'  (score {h['score']})")


def _json_default(o):
    # make Decimal/date/etc serializable
    from datetime import date, datetime
    from decimal import Decimal
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return str(o)


def cmd_list(fmt):
    cat = _load_catalog()
    tables = cat.get("tables", {})
    if fmt == "json":
        print(json.dumps(cat, indent=2, default=_json_default))
        return
    if not tables:
        print("(no tables registered)")
        return
    for name, e in tables.items():
        print(f"- {name}  [{e.get('file_type')}]  rows={e.get('row_count')}")
        if e.get("description"):
            print(f"    desc: {e['description']}")
        if e.get("keywords"):
            print(f"    keywords: {', '.join(e['keywords'])}")
        cols = e.get("columns", [])
        print("    columns: " + ", ".join(f"{c['name']}:{c['type']}" for c in cols))
        sem = e.get("semantic_indexed") or []
        if sem:
            print("    semantic-indexed (prose): " + ", ".join(sem)
                  + "  — query with --semantic \"" + name + "." + sem[0] + ": <text>\"")


def cmd_table(name, fmt):
    cat = _load_catalog()
    e = cat.get("tables", {}).get(name)
    if e is None:
        _die(f"table '{name}' not in catalog. Use --list to see registered tables.", 2)
    if fmt == "json":
        print(json.dumps(e, indent=2, default=_json_default))
        return
    print(f"table: {name}  [{e.get('file_type')}]  rows={e.get('row_count')}")
    if e.get("description"):
        print(f"desc: {e['description']}")
    if e.get("keywords"):
        print(f"keywords: {', '.join(e['keywords'])}")
    print("columns:")
    for c in e.get("columns", []):
        print(f"  {c['name']}: {c['type']}")


def _parse_semantic_arg(raw, table_arg, column_arg):
    """Accept either "<table>.<col>: <query>" / "<col>: <query>" inline, or a bare
    <query> plus --table/--column. Returns (table, column, query) with table/column
    possibly None (resolved against the catalog later)."""
    table, column, query = table_arg, column_arg, raw
    if ":" in raw:
        head, q = raw.split(":", 1)
        head, q = head.strip(), q.strip()
        if q:
            query = q
            if "." in head:
                t, c = head.split(".", 1)
                table = table or t.strip()
                column = column or c.strip()
            elif head:
                column = column or head
    return table, column, query.strip()

def _resolve_semantic_target(cat, table, column):
    """Pick the (table, column) prose index to search. If ambiguous/missing, error with
    the list of available semantic-indexed columns so the caller can disambiguate."""
    avail = []  # (table, column)
    for tname, e in cat.get("tables", {}).items():
        for c in e.get("semantic_indexed", []) or []:
            avail.append((tname, c))
    if not avail:
        _die("no semantic text index exists yet. Register a dataset with a free-text (prose) "
             "column first (data_register.py builds the index automatically).", 2)
    # filter by provided table/column
    cands = [tc for tc in avail if (table is None or tc[0] == table) and (column is None or tc[1] == column)]
    if not cands:
        listing = ", ".join(f"{t}.{c}" for t, c in avail)
        _die(f"no semantic-indexed column matches table={table} column={column}. "
             f"Available: {listing}", 2)
    if len(cands) > 1:
        listing = ", ".join(f"{t}.{c}" for t, c in cands)
        _die(f"ambiguous semantic target ({listing}). Specify with \"<table>.<col>: <query>\" "
             f"or --table/--column.", 2)
    return cands[0]

def cmd_semantic(raw, table_arg, column_arg, fmt, k=5):
    """Rank rows of a prose column by semantic similarity to a query, then join the
    matched rowids back to full DuckDB rows. Optional-dep guarded: if the embedding
    stack is absent, exit 3 cleanly (exact SQL path is unaffected)."""
    sys.path.insert(0, HERE)  # tools/ — for _embed
    try:
        import _embed
    except Exception as e:
        _die(f"semantic rank unavailable (embed helper not importable: {e}). Exact SQL is unaffected.", 3)
    if not _embed.semantic_available():
        _die("semantic rank unavailable — install: pip install langchain-chroma "
             "langchain-huggingface sentence-transformers. Exact SQL is unaffected.", 3)
    cat = _load_catalog()
    table, column, query = _parse_semantic_arg(raw, table_arg, column_arg)
    table, column = _resolve_semantic_target(cat, table, column)
    if not query:
        _die("empty semantic query. Use --semantic \"<table>.<col>: <what you're looking for>\".", 2)
    cname = f"{table}__{column}"
    persist = os.path.join(TEXT_INDEX_DIR, cname)
    if not os.path.isdir(persist):
        _die(f"index dir missing for {table}.{column} ({persist}). Re-register/refresh the table.", 2)
    try:
        vs = _embed.get_chroma(persist, cname)
        # Fail loud if this prose index was built with a different embedding model
        # than the one now configured (else ranking silently misaligns -> garbage).
        _embed.warn_on_mismatch(vs, where=f"data_query:{table}.{column}")
        hits = vs.similarity_search_with_score(query, k=k)
    except _embed.SemanticUnavailable as e:
        _die(f"semantic rank unavailable ({e}). Exact SQL is unaffected.", 3)
    except Exception as e:
        _die(f"semantic search failed: {e}", 2)
    # map rowids -> full rows (preserve rank order). chroma score = distance (lower=closer).
    ranked = []
    for doc, score in hits:
        rid = doc.metadata.get("rowid")
        ranked.append({"rowid": rid, "distance": float(score), "text": doc.page_content})
    if not ranked:
        if fmt == "json":
            print(json.dumps({"table": table, "column": column, "query": query, "results": []}, indent=2))
        else:
            print("(0 semantic matches)")
        return
    rids = [r["rowid"] for r in ranked if r["rowid"] is not None]
    fullrows = {}
    if rids:
        con = _connect_ro()
        try:
            placeholders = ",".join("?" for _ in rids)
            cur = con.execute(f'SELECT rowid AS _rid, * FROM "{table}" WHERE rowid IN ({placeholders})', rids)
            colnames = [d[0] for d in cur.description]
            for row in cur.fetchall():
                d = dict(zip(colnames, row))
                fullrows[d.pop("_rid")] = d
        finally:
            con.close()
    results = []
    for r in ranked:
        merged = {"_score": round(1.0 / (1.0 + r["distance"]), 4), "_distance": round(r["distance"], 4)}
        merged.update(fullrows.get(r["rowid"], {column: r["text"]}))
        results.append(merged)
    if fmt == "json":
        print(json.dumps({"table": table, "column": column, "query": query, "results": results},
                         indent=2, default=_json_default))
        return
    print(f"semantic rank on {table}.{column}  query={query!r}  (top {len(results)})")
    for i, r in enumerate(results, 1):
        txt = str(r.get(column, ""))
        preview = (txt[:90] + "…") if len(txt) > 90 else txt
        others = {kk: vv for kk, vv in r.items() if kk not in ("_score", "_distance", column)}
        extra = "  ".join(f"{kk}={vv}" for kk, vv in others.items())
        print(f"  {i}. score={r['_score']:.3f}  {extra}")
        print(f"       {column}: {preview}")

def cmd_query(sql, table_scope, fmt):
    try:
        rows = data_query(sql)
    except QueryError as e:
        _die(str(e), 2)
    # optional scope hint: if --table given, verify it's referenced (soft check, informational)
    if table_scope and table_scope not in sql:
        sys.stderr.write(f"data_query: note — --table {table_scope} given but not referenced in SQL.\n")
    if fmt == "json":
        print(json.dumps(rows, indent=2, default=_json_default))
        return
    if not rows:
        print("(0 rows)")
        return
    cols = list(rows[0].keys())
    print(" | ".join(cols))
    print("-" * (sum(len(c) for c in cols) + 3 * (len(cols) - 1)))
    for r in rows:
        print(" | ".join(str(r[c]) for c in cols))
    print(f"({len(rows)} rows)")


def build_parser():
    p = argparse.ArgumentParser(description="Read-only exact SQL retrieval over the neuron structured-data store.")
    p.add_argument("--sql", help="a single read-only SELECT/WITH query")
    p.add_argument("--list", action="store_true", help="list catalog tables + schema + keywords")
    p.add_argument("--table", help="scope/show a single table (with --sql: soft scope hint; alone: show schema)")
    p.add_argument("--column", help="restrict --suggest to one column")
    p.add_argument("--suggest", help="resolve an inexact filter VALUE (typo/synonym/partial) to real stored values")
    p.add_argument("--semantic", help='rank prose rows by meaning: "<table>.<col>: <query>" (or bare query + --table/--column)')
    p.add_argument("-k", type=int, default=5, help="number of semantic results (default 5)")
    p.add_argument("--format", choices=["text", "json"], default="text", help="output format (default text)")
    return p


def _obs_data(query, extra):  # retrieval observability (fail-open)
    try:
        import sys as _s
        from pathlib import Path as _P
        ws = _P(__file__).resolve().parent.parent
        _s.path.insert(0, str(ws / "procedures"))
        from _retrieval_log import log_retrieval
        log_retrieval(ws, "data_query", query, extra=extra)
    except Exception:
        pass

def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list:
        return cmd_list(args.format)
    if args.suggest:
        return cmd_suggest(args.suggest, args.table, args.column, args.format)
    if args.semantic:
        _obs_data(args.semantic, {"mode": "semantic", "table": args.table})
        return cmd_semantic(args.semantic, args.table, args.column, args.format, k=args.k)
    if args.sql:
        _obs_data(args.sql, {"mode": "sql", "table": args.table})
        return cmd_query(args.sql, args.table, args.format)
    if args.table:
        return cmd_table(args.table, args.format)
    _die("nothing to do. Use --sql \"SELECT ...\", --list, --table <name>, or --suggest <value>.", 2)


if __name__ == "__main__":
    main()
