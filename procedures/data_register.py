#!/usr/bin/env python3
"""
data_register.py — register a structured dataset (csv|json|parquet) into the
neuron's structured-data store (exact-SQL PRIMARY, with optional per-prose-column semantic rank).

Materializes the source file INTO one shared DuckDB (kb/data_store/data.duckdb)
as a single table (one table per dataset = a "category"), infers column types,
and writes/updates the routing entry in kb/data_store/catalog.json.

Retrieval is PRIMARILY exact SQL (see tools/data_query.py): filter/range/sort/COUNT/SUM/GROUP BY/JOIN.
PLUS two optional aids: (a) fuzzy value routing (--suggest, no embeddings) to resolve inexact filter
values, and (b) an OPTIONAL per-column semantic rank over free-PROSE text columns (--semantic): flagged
prose columns get a small embedding index (multilingual-e5-small, reused from docs_search) so you can "find
rows about X". Semantic is strictly opt-in per prose column and optional-dep guarded — if the embedding
stack is absent, the exact-SQL path is completely unaffected.

CLI:
  data_register.py --file sales.csv --name sales --desc "Q2 sales export" [--keywords a,b,c] [--source-date 2026-06-30]
  data_register.py --list
  data_register.py --dry-run --file x.json --name x --desc "..."
  data_register.py --refresh            # re-scan every registered source; update schema/row_count; prune vanished
  data_register.py --refresh --name x   # refresh one table only

Design notes:
  - Store: ONE data.duckdb, ONE table per dataset. NOT a category column, NOT a db-per-category.
  - catalog.json is the schema map AND intent router (keywords + columns per table).
  - Register ALWAYS materializes (CREATE TABLE AS SELECT FROM 'file'); read-in-place happens
    only at register time via DuckDB's file readers.
  - Hash-based skip: on --refresh, unchanged source files (same content hash) are skipped.
  - duckdb is import-guarded: if missing, the tool exits with a clear hint (feature unavailable),
    it does not crash the wider neuron.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

# ── import guard (feature-unavailable, not a crash) ──────────────────────────
try:
    import duckdb
except ImportError:
    sys.stderr.write(
        "data_register: duckdb not installed — structured_data feature unavailable.\n"
        "Install with: python3 -m pip install duckdb  (semantic tools are unaffected)\n"
    )
    sys.exit(3)

HERE = os.path.dirname(os.path.abspath(__file__))
# procedures/ is a sibling of kb/ inside the neuron package
STORE_DIR = os.path.normpath(os.path.join(HERE, "..", "kb", "data_store"))
DB_PATH = os.path.join(STORE_DIR, "data.duckdb")
CATALOG_PATH = os.path.join(STORE_DIR, "catalog.json")
TEXT_INDEX_DIR = os.path.join(STORE_DIR, "text_index")

# A prose column is a free-text column worth semantic indexing (survey feedback,
# reviews, tickets) — NOT a low-cardinality categorical dimension and NOT short codes.
PROSE_MIN_AVG_LEN = 25    # avg char length above which a text column counts as "prose"
PROSE_MIN_DISTINCT = 5    # need some variety; a near-constant text col isn't worth embedding

VALID_EXT = {".csv": "csv", ".json": "json", ".parquet": "parquet"}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _die(msg, code=1):
    sys.stderr.write("data_register: " + msg + "\n")
    sys.exit(code)


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in VALID_EXT:
        _die(f"unsupported file type '{ext}'. Supported: csv, json, parquet.", 2)
    return VALID_EXT[ext]


def _valid_table_name(name):
    if not _IDENT_RE.match(name):
        _die(
            f"invalid --name '{name}'. Use a SQL-safe identifier: "
            "letters/digits/underscore, not starting with a digit.",
            2,
        )
    return name


def _reader_expr(path, ftype):
    """DuckDB reader expression for the source file. union_by_name/maximum_depth
    help with irregular/nested JSON."""
    p = path.replace("'", "''")
    if ftype == "csv":
        return f"read_csv_auto('{p}', header=true, sample_size=-1)"
    if ftype == "json":
        # auto handles arrays of objects, ndjson, and nested structs
        return f"read_json_auto('{p}', maximum_depth=-1)"
    if ftype == "parquet":
        return f"read_parquet('{p}')"
    _die(f"no reader for type {ftype}", 2)


def _ensure_store():
    os.makedirs(STORE_DIR, exist_ok=True)


def _load_catalog():
    if not os.path.exists(CATALOG_PATH):
        return {
            "_schema_version": 1,
            "_doc": "Intent router + schema map for the structured-data (SQL RAG) store.",
            "tables": {},
        }
    with open(CATALOG_PATH, "r") as f:
        cat = json.load(f)
    cat.setdefault("tables", {})
    return cat


def _save_catalog(cat):
    tmp = CATALOG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cat, f, indent=2)
    os.replace(tmp, CATALOG_PATH)


def _describe_columns(con, table):
    """Return [{name, type, desc}] from DuckDB's table schema."""
    rows = con.execute(f'DESCRIBE "{table}"').fetchall()
    # DESCRIBE columns: column_name, column_type, null, key, default, extra
    cols = []
    for r in rows:
        cols.append({"name": r[0], "type": r[1], "desc": ""})
    return cols


def _has_text_column(columns):
    text_types = ("VARCHAR", "TEXT", "STRING", "CHAR")
    return any(any(t in c["type"].upper() for t in text_types) for c in columns)

# ── prose detection + semantic text-index (optional; free-text columns only) ─────
def _prose_text_columns(con, table, cols):
    """Mark and return columns that are free-prose (semantic-indexable). Stamps c['is_prose'].
    A column is prose when: text type, NOT categorical (a dimension is codes, not prose),
    has enough distinct values, and a meaningful average length. Best-effort per column."""
    prose = []
    for c in cols:
        c["is_prose"] = False
        up = c["type"].upper()
        if not any(t in up for t in ("VARCHAR", "TEXT", "STRING", "CHAR")):
            continue
        if c.get("categorical"):
            continue  # a routing dimension (region, tier) is not prose
        q = c["name"].replace('"', '""')
        try:
            avg_len, dc = con.execute(
                f'SELECT avg(length("{q}")), count(DISTINCT "{q}") FROM "{table}"'
            ).fetchone()
        except Exception:
            continue
        if avg_len is None:
            continue
        if float(avg_len) >= PROSE_MIN_AVG_LEN and (dc or 0) >= PROSE_MIN_DISTINCT:
            c["is_prose"] = True
            prose.append(c["name"])
    return prose

def _collection_name(table, column):
    return f"{table}__{column}"

def _drop_text_index(table, column=None):
    """Remove the on-disk chroma collection(s) for a table (or one column)."""
    import shutil
    if not os.path.isdir(TEXT_INDEX_DIR):
        return
    # collections are stored inside one chroma persist dir keyed by collection_name;
    # we use a per-collection subdir so drop = rmtree of that subdir.
    prefix = f"{table}__" if column is None else _collection_name(table, column)
    for name in os.listdir(TEXT_INDEX_DIR):
        if (column is None and name.startswith(prefix)) or (column is not None and name == prefix):
            shutil.rmtree(os.path.join(TEXT_INDEX_DIR, name), ignore_errors=True)

def _build_text_index(con, table, cols, quiet=False):
    """Embed each prose column's row values into a per-column Chroma collection under
    TEXT_INDEX_DIR/<table>__<col>/, storing rowid so a hit maps back to the real row.
    Optional: if the embedding stack is missing, prints a note and returns [] WITHOUT
    failing registration (exact-SQL path is unaffected). Returns list of indexed columns."""
    prose_cols = [c["name"] for c in cols if c.get("is_prose")]
    if not prose_cols:
        return []
    # import the shared resolver lazily so a missing optional dep never breaks register
    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
    try:
        import _embed
    except Exception:
        if not quiet:
            print("     [semantic] embed helper not importable; skipped text index (exact SQL unaffected).")
        return []
    if not _embed.semantic_available():
        if not quiet:
            print("     [semantic] langchain/chroma not installed; skipped text index "
                  "(pip install langchain-chroma langchain-huggingface sentence-transformers). Exact SQL unaffected.")
        return []
    os.makedirs(TEXT_INDEX_DIR, exist_ok=True)
    indexed = []
    for col in prose_cols:
        q = col.replace('"', '""')
        try:
            rows = con.execute(
                f'SELECT rowid AS _rid, "{q}" AS _txt FROM "{table}" WHERE "{q}" IS NOT NULL'
            ).fetchall()
        except Exception as e:
            if not quiet:
                print(f"     [semantic] {table}.{col}: could not read rows ({e}); skipped.")
            continue
        if not rows:
            continue
        _drop_text_index(table, col)  # rebuild fresh
        cname = _collection_name(table, col)
        persist = os.path.join(TEXT_INDEX_DIR, cname)
        try:
            vs = _embed.get_chroma(persist, cname)
            texts = [str(r[1]) for r in rows]
            metadatas = [{"table": table, "column": col, "rowid": int(r[0])} for r in rows]
            vs.add_texts(texts=texts, metadatas=metadatas)
            indexed.append(col)
            if not quiet:
                print(f"     [semantic] indexed {table}.{col}: {len(texts)} rows -> {cname}")
        except _embed.SemanticUnavailable as e:
            if not quiet:
                print(f"     [semantic] unavailable: {e}. Exact SQL unaffected.")
            return indexed
        except Exception as e:
            if not quiet:
                print(f"     [semantic] {table}.{col}: index build failed ({e}); skipped.")
    return indexed

# ── catalog auto-enrichment (routing + correct-WHERE aid; pure metadata, no LLM) ──
# Per column: distinct_count + (for low-cardinality dimensions) a few sample values.
# Auto-keywords: table name + column names + sampled categorical values. This makes
# a NL request route to the right table even without hand-typed --keywords, and gives
# the agent the value vocabulary to write correct WHERE clauses.
CATEG_MAX = 25          # a column with <= this many distinct values is a routing "dimension"
SAMPLE_VALUES = 12      # max distinct values sampled per categorical column
_CATEG_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR", "BOOLEAN", "ENUM")
_KW_STOP = {"id", "name", "date", "created", "updated", "time", "timestamp", "value", "desc", "description"}

_INT_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "HUGEINT")

def _is_text_type(coltype):
    return any(x in coltype.upper() for x in ("VARCHAR", "TEXT", "STRING", "CHAR", "ENUM"))

def _is_int_type(coltype):
    t = coltype.upper()
    return any(x in t for x in _INT_TYPES) and "DECIMAL" not in t

def _is_categorical_type(coltype):
    t = coltype.upper()
    if any(x in t for x in _CATEG_TYPES):
        return True
    # small-domain integer codes (e.g. tier=1..3) count too; floats/decimals do not
    return _is_int_type(t)

def _tokenize(s):
    return [t for t in re.split(r"[^A-Za-z0-9]+", str(s).lower()) if t and not t.isdigit()]

def _profile_columns(con, table, cols):
    """Mutate cols in place: add distinct_count, categorical, sample_values per column.
    STRUCT/nested columns are profiled shallowly (distinct on the whole struct is skipped).
    Best-effort: profiling failure on one column never aborts registration."""
    try:
        row_count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except Exception:
        row_count = None
    for c in cols:
        name, ctype = c["name"], c["type"]
        c["distinct_count"] = None
        c["categorical"] = False
        c["sample_values"] = []
        up = ctype.upper()
        if up.startswith("STRUCT") or up.startswith("MAP") or up.startswith("LIST") or "[]" in up:
            continue  # nested/complex: skip value profiling (query via dotted path instead)
        q = name.replace('"', '""')
        try:
            dc = con.execute(f'SELECT COUNT(DISTINCT "{q}") FROM "{table}"').fetchone()[0]
        except Exception:
            continue
        c["distinct_count"] = dc
        # An integer column that is (near-)unique is an identifier/key, not a routing
        # dimension — exclude it so ids don't masquerade as categoricals.
        is_int = _is_int_type(ctype) and not _is_text_type(ctype)
        if is_int and dc is not None and row_count and dc >= max(1, int(0.9 * row_count)):
            continue
        # A long-form TEXT column is PROSE (feedback/reviews/notes), not a routing
        # dimension — even at low distinct count. Exclude it from categorical so it can
        # instead be picked up as a semantic-indexable prose column. Gate on avg length.
        if _is_text_type(ctype):
            try:
                avg_len = con.execute(f'SELECT avg(length("{q}")) FROM "{table}"').fetchone()[0]
            except Exception:
                avg_len = None
            if avg_len is not None and float(avg_len) >= PROSE_MIN_AVG_LEN:
                continue  # prose, not a dimension
        if _is_categorical_type(ctype) and dc is not None and 0 < dc <= CATEG_MAX:
            c["categorical"] = True
            try:
                rows = con.execute(
                    f'SELECT DISTINCT "{q}" FROM "{table}" WHERE "{q}" IS NOT NULL '
                    f'ORDER BY 1 LIMIT {SAMPLE_VALUES}'
                ).fetchall()
                c["sample_values"] = [r[0] for r in rows]
            except Exception:
                c["sample_values"] = []
    return cols

def _auto_keywords(table, cols):
    """Derive routing keywords from table name + column names + sampled categorical values.
    Returns a deduped, order-preserving list (all lowercased)."""
    kws = []
    seen = set()
    def _add(tok):
        tok = str(tok).strip().lower()
        if tok and tok not in seen and len(tok) <= 40:
            seen.add(tok)
            kws.append(tok)
    for t in _tokenize(table):
        _add(t)
    for c in cols:
        cn = c["name"].lower()
        if cn not in _KW_STOP:
            _add(cn)
            for t in _tokenize(cn):
                _add(t)
        # Only WORD-like categorical values become routing keywords. Numeric/id values
        # are noise for NL routing (kept in sample_values for WHERE-writing, not here).
        if c.get("categorical") and _is_text_type(c["type"]):
            for v in c.get("sample_values") or []:
                for t in _tokenize(v):
                    _add(t)
    return kws

def _merge_keywords(manual, auto):
    """Manual keywords first (author intent wins ordering), then auto, deduped."""
    out, seen = [], set()
    for k in list(manual) + list(auto):
        kl = str(k).strip().lower()
        if kl and kl not in seen:
            seen.add(kl)
            out.append(kl)
    return out


def _materialize(con, table, path, ftype):
    """CREATE OR REPLACE TABLE <table> AS SELECT * FROM <reader>. Returns row_count."""
    expr = _reader_expr(path, ftype)
    con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM {expr}')
    return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def cmd_register(args):
    path = os.path.abspath(args.file)
    if not os.path.exists(path):
        _die(f"file not found: {path}", 2)
    if os.path.getsize(path) == 0:
        _die(f"file is empty: {path}", 2)
    ftype = _file_type(path)
    table = _valid_table_name(args.name)
    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
    src_hash = _file_hash(path)

    if args.dry_run:
        # Validate readability without writing anything.
        con = duckdb.connect(":memory:")
        try:
            n = _materialize(con, table, path, ftype)
            cols = _describe_columns(con, table)
            _profile_columns(con, table, cols)
        except Exception as e:
            _die(f"dry-run: source not readable by DuckDB: {e}", 2)
        finally:
            con.close()
        auto_kw = _auto_keywords(table, cols)
        merged_kw = _merge_keywords(keywords, auto_kw)
        print(f"[dry-run] OK — would register table '{table}' from {ftype} file")
        print(f"[dry-run] rows={n} cols={len(cols)}: " + ", ".join(f"{c['name']}:{c['type']}" for c in cols))
        categ = [f"{c['name']}({c['distinct_count']})" for c in cols if c.get('categorical')]
        print("[dry-run] categorical dims: " + (", ".join(categ) if categ else "(none)"))
        print("[dry-run] auto-keywords: " + (", ".join(merged_kw) if merged_kw else "(none)"))
        print("[dry-run] no DB write, no catalog write.")
        return

    _ensure_store()
    cat = _load_catalog()
    con = duckdb.connect(DB_PATH)
    try:
        n = _materialize(con, table, path, ftype)
        cols = _describe_columns(con, table)
        _profile_columns(con, table, cols)
        prose_cols = _prose_text_columns(con, table, cols)
    except Exception as e:
        con.close()
        _die(f"register failed (source not readable): {e}", 2)
    con.close()

    merged_kw = _merge_keywords(keywords, _auto_keywords(table, cols))
    entry = {
        "table_name": table,
        "source_file": path,
        "file_type": ftype,
        "columns": cols,
        "row_count": n,
        "registered_at": _utc_now(),
        "source_date": args.source_date,
        "description": args.desc or "",
        "keywords": merged_kw,
        "source_hash": src_hash,
        "has_text_column": _has_text_column(cols),
        "prose_columns": prose_cols,
        "semantic_indexed": [],
    }
    # Build the semantic text index for prose columns (optional; deps-guarded).
    if prose_cols and not getattr(args, "no_semantic", False):
        con_ro = duckdb.connect(DB_PATH, read_only=True)
        try:
            entry["semantic_indexed"] = _build_text_index(con_ro, table, cols)
        finally:
            con_ro.close()
    elif prose_cols:
        print(f"     [semantic] --no-semantic set; {len(prose_cols)} prose column(s) NOT indexed.")

    existed = table in cat["tables"]
    cat["tables"][table] = entry
    _save_catalog(cat)
    verb = "re-registered" if existed else "registered"
    print(f"OK — {verb} table '{table}' ({ftype}, {n} rows, {len(cols)} cols) into {DB_PATH}")
    print("     catalog updated: " + CATALOG_PATH)
    if prose_cols:
        print(f"     prose columns: {', '.join(prose_cols)}"
              + (f" | semantic-indexed: {', '.join(entry['semantic_indexed'])}" if entry["semantic_indexed"] else " | not indexed"))


def cmd_list(args):
    cat = _load_catalog()
    tables = cat.get("tables", {})
    if not tables:
        print("(no tables registered)")
        return
    for name, e in tables.items():
        print(f"- {name}  [{e.get('file_type')}]  rows={e.get('row_count')}  cols={len(e.get('columns', []))}")
        if e.get("description"):
            print(f"    desc: {e['description']}")
        if e.get("keywords"):
            print(f"    keywords: {', '.join(e['keywords'])}")
        dims = [c for c in e.get("columns", []) if c.get("categorical")]
        for c in dims:
            vals = c.get("sample_values") or []
            shown = ", ".join(str(v) for v in vals[:8])
            more = " …" if len(vals) > 8 else ""
            print(f"    dim {c['name']} ({c.get('distinct_count')} distinct): {shown}{more}")
        print(f"    source: {e.get('source_file')}")


def cmd_refresh(args):
    cat = _load_catalog()
    tables = cat.get("tables", {})
    if not tables:
        print("(nothing to refresh — no tables registered)")
        return
    targets = [args.name] if args.name else list(tables.keys())
    _ensure_store()
    con = duckdb.connect(DB_PATH)
    changed, skipped, pruned = [], [], []
    reindex = []  # tables whose prose index must be rebuilt after the write conn closes
    try:
        for name in targets:
            e = tables.get(name)
            if e is None:
                _die(f"--refresh --name '{name}': not in catalog", 2)
            src = e["source_file"]
            if not os.path.exists(src):
                # prune vanished (table + its semantic index)
                try:
                    con.execute(f'DROP TABLE IF EXISTS "{name}"')
                except Exception:
                    pass
                _drop_text_index(name)
                del cat["tables"][name]
                pruned.append(name)
                continue
            new_hash = _file_hash(src)
            if new_hash == e.get("source_hash"):
                skipped.append(name)
                continue
            ftype = e["file_type"]
            n = _materialize(con, name, src, ftype)
            cols = _describe_columns(con, name)
            _profile_columns(con, name, cols)
            prose_cols = _prose_text_columns(con, name, cols)
            e["row_count"] = n
            e["columns"] = cols
            e["source_hash"] = new_hash
            e["registered_at"] = _utc_now()
            e["has_text_column"] = _has_text_column(cols)
            e["prose_columns"] = prose_cols
            # refresh keywords: preserve prior (incl. any manual) + fresh auto, deduped
            e["keywords"] = _merge_keywords(e.get("keywords", []), _auto_keywords(name, cols))
            changed.append(name)
            if prose_cols and not getattr(args, "no_semantic", False):
                reindex.append(name)
            elif not prose_cols:
                _drop_text_index(name)  # no longer prose -> remove stale index
                e["semantic_indexed"] = []
    finally:
        con.close()
    # rebuild semantic indexes for changed prose tables (read-only conn; deps-guarded)
    if reindex:
        con_ro = duckdb.connect(DB_PATH, read_only=True)
        try:
            for name in reindex:
                e = cat["tables"][name]
                e["semantic_indexed"] = _build_text_index(con_ro, name, e["columns"])
        finally:
            con_ro.close()
    _save_catalog(cat)
    print(f"refresh done — updated: {changed or '[]'}  skipped(unchanged): {skipped or '[]'}  pruned(vanished): {pruned or '[]'}")


def build_parser():
    p = argparse.ArgumentParser(description="Register structured datasets into the neuron exact-SQL store.")
    p.add_argument("--file", help="source file (csv|json|parquet)")
    p.add_argument("--name", help="table name (SQL-safe identifier)")
    p.add_argument("--desc", help="one-line dataset description (routing)")
    p.add_argument("--keywords", help="comma-separated intent-routing keywords")
    p.add_argument("--source-date", dest="source_date", default=None, help="logical date of the data (YYYY-MM-DD)")
    p.add_argument("--no-semantic", dest="no_semantic", action="store_true", help="skip building the semantic text index for prose columns")
    p.add_argument("--dry-run", action="store_true", help="validate readability; no DB/catalog write")
    p.add_argument("--refresh", action="store_true", help="re-scan registered sources; update schema/rows; prune vanished")
    p.add_argument("--list", action="store_true", help="list registered tables")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list:
        return cmd_list(args)
    if args.refresh:
        return cmd_refresh(args)
    if args.file or args.name:
        if not args.file:
            _die("--file is required to register", 2)
        if not args.name:
            _die("--name is required to register", 2)
        return cmd_register(args)
    _die("nothing to do. Use --file+--name to register, or --list / --refresh / --dry-run.", 2)


if __name__ == "__main__":
    main()
