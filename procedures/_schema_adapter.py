"""Schema adapter for the OpenClaw native memory index (sqlite).

Why this exists
---------------
neuron's L2/L3 (memory_graph.py, memory_synthesis.py) read the sqlite index that
OpenClaw's native memory search builds at ~/.openclaw/memory/<agent-id>.sqlite.
That schema is owned by OpenClaw, not by dinomem — so it can DRIFT across
OpenClaw versions. On the reference host the tables are `files(path,hash,...)`
and `chunks(id,path,start_line,end_line,text,embedding,...)`. Some OpenClaw
builds instead expose a `memory_index_chunks`-style layout (different table and
column names). When that happens, neuron's hard-coded `SELECT ... FROM chunks`
throws `no such table: chunks` (or `no such column: ...`) and L2/L3 go dark
while every other cron keeps running.

This module resolves the real table/column names at RUNTIME by introspecting
`sqlite_master` + `PRAGMA table_info`, using a table of known aliases. If it
cannot find a workable mapping it raises SchemaUnsupported carrying the FULL
schema dump, so an unknown variant is diagnosable in one shot instead of guessed
at. No column names are assumed blindly — every mapping is verified to exist in
the live DB before any query is built.

It does NOT invent data. If the index has no per-chunk embeddings, callers that
need vectors get an explicit, actionable error rather than silent garbage.
"""

import sqlite3


class SchemaUnsupported(RuntimeError):
    """Raised when the sqlite index matches no known memory-index schema.

    Carries the introspected schema so the failure is self-diagnosing.
    """


# ── Known column aliases, in priority order ─────────────────────────────────
# First existing column in each list wins. Extend these lists (not the query
# sites) when a new OpenClaw schema variant appears.
_FILES_TABLE_ALIASES = ["files", "memory_index_files", "memory_files"]
_CHUNKS_TABLE_ALIASES = ["chunks", "memory_index_chunks", "memory_chunks"]

_COL = {
    # canonical -> candidate column names (priority order)
    "chunk_id": ["id", "chunk_id", "rowid"],
    "path": ["path", "file_path", "source_path", "file"],
    "start_line": ["start_line", "start", "line_start", "startLine"],
    "end_line": ["end_line", "end", "line_end", "endLine"],
    "text": ["text", "content", "chunk_text", "body"],
    "embedding": ["embedding", "vector", "embeddings", "vec", "emb"],
    "hash": ["hash", "content_hash", "sha", "sha256", "digest"],
}


def _tables(con):
    return {
        r[0]
        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(con, table):
    # table name is chosen from a fixed allow-list above, never user input.
    # Virtual tables (FTS5, sqlite-vec's vec0) can raise 'no such module' on
    # PRAGMA when the loadable extension isn't present in this python's sqlite.
    # Those are never our data tables, so treat them as introspectable-empty.
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return []


def _pick_table(available, aliases):
    for name in aliases:
        if name in available:
            return name
    return None


def _pick_col(cols_lower_map, canonical, *, required, table, dump):
    for cand in _COL[canonical]:
        if cand.lower() in cols_lower_map:
            return cols_lower_map[cand.lower()]
    if required:
        raise SchemaUnsupported(
            f"memory index table '{table}' has no column for '{canonical}' "
            f"(tried {_COL[canonical]}).\n{dump}"
        )
    return None


def _dump(con):
    lines = ["--- memory index schema dump ---"]
    for t in sorted(_tables(con)):
        cols = _columns(con, t)
        lines.append(f"  {t}({', '.join(cols)})")
    lines.append(
        "Report this dump to the dinomem developer (t.me/dinotlgrm) so this "
        "OpenClaw schema variant can be added to _schema_adapter.py."
    )
    return "\n".join(lines)


def resolve(db_path):
    """Introspect db_path and return a Schema describing how to read it.

    Raises SchemaUnsupported (with a full schema dump) if no mapping fits.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        available = _tables(con)
        dump = _dump(con)

        files_tbl = _pick_table(available, _FILES_TABLE_ALIASES)
        chunks_tbl = _pick_table(available, _CHUNKS_TABLE_ALIASES)
        if chunks_tbl is None:
            raise SchemaUnsupported(
                "no recognizable chunks table "
                f"(looked for {_CHUNKS_TABLE_ALIASES}).\n{dump}"
            )

        ccols = _columns(con, chunks_tbl)
        ccols_map = {c.lower(): c for c in ccols}
        chunk = {
            "table": chunks_tbl,
            "id": _pick_col(ccols_map, "chunk_id", required=True, table=chunks_tbl, dump=dump),
            "path": _pick_col(ccols_map, "path", required=True, table=chunks_tbl, dump=dump),
            "start_line": _pick_col(ccols_map, "start_line", required=False, table=chunks_tbl, dump=dump),
            "end_line": _pick_col(ccols_map, "end_line", required=False, table=chunks_tbl, dump=dump),
            "text": _pick_col(ccols_map, "text", required=True, table=chunks_tbl, dump=dump),
            "embedding": _pick_col(ccols_map, "embedding", required=False, table=chunks_tbl, dump=dump),
        }

        files = None
        if files_tbl is not None:
            fcols_map = {c.lower(): c for c in _columns(con, files_tbl)}
            fpath = _pick_col(fcols_map, "path", required=False, table=files_tbl, dump=dump)
            fhash = _pick_col(fcols_map, "hash", required=False, table=files_tbl, dump=dump)
            if fpath and fhash:
                files = {"table": files_tbl, "path": fpath, "hash": fhash}

        return Schema(chunk=chunk, files=files, dump=dump)
    finally:
        con.close()


class Schema:
    def __init__(self, chunk, files, dump):
        self.chunk = chunk
        self.files = files
        self.dump = dump

    # ── signature (files table) ─────────────────────────────────────────────
    def signature_sql(self):
        """SQL for the content signature, or None if no usable files table.

        Callers should fall back to a chunks-based signature when this is None.
        """
        if not self.files:
            return None
        f = self.files
        return f"SELECT {f['path']}, {f['hash']} FROM {f['table']} ORDER BY {f['path']}"

    def chunk_signature_sql(self):
        """Fallback signature from chunks when there is no files table."""
        c = self.chunk
        hash_expr = c["text"]  # hash of text stands in when no per-file hash
        return f"SELECT {c['path']}, {hash_expr} FROM {c['table']} ORDER BY {c['path']}"

    # ── load_chunks ─────────────────────────────────────────────────────────
    def load_chunks_sql(self):
        """SELECT returning (id, path, start_line, end_line, text, embedding).

        Missing optional columns are emitted as NULL so downstream tuple
        unpacking stays positionally stable.
        """
        c = self.chunk
        if not c["embedding"]:
            raise SchemaUnsupported(
                f"chunks table '{c['table']}' has no embedding column "
                f"(tried {_COL['embedding']}). L2/L3 need per-chunk vectors from "
                "the OpenClaw memory index; this build does not store them.\n"
                f"{self.dump}"
            )
        start = c["start_line"] or "NULL"
        end = c["end_line"] or "NULL"
        return (
            f"SELECT {c['id']}, {c['path']}, {start}, {end}, {c['text']}, "
            f"{c['embedding']} FROM {c['table']}"
        )

    # ── synthesis: fetch text for a set of chunk ids ────────────────────────
    def chunk_text_sql(self, n_ids):
        c = self.chunk
        qmarks = ",".join("?" * n_ids)
        return (
            f"SELECT {c['id']}, {c['path']}, {c['text']} "
            f"FROM {c['table']} WHERE {c['id']} IN ({qmarks})"
        )
