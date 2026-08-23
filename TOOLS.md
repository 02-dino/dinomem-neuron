# TOOLS.md
# What tools exist + when/how to call them. Fields returned by a tool come back
# in its response — not pre-declared here. Kept: name/path/purpose, subcommand
# enums, required inputs, non-obvious defaults, and interpretive caveats that
# shape analysis quality. Cut: dep lists, cache TTLs, importable signatures,
# mechanical field enumerations, env-tuning tables.

tools:

  session_search:
    path: tools/session_search.py
    type: exec
    db: kb/vector_db_sessions
    purpose: "Deep recall over archived sessions (semantic + recency rerank). <7d transcript detail when memory_search is thin."
    inputs:
      query: {type: string, note: "Positional. Required unless --list. Natural language (M3), not code identifiers."}
      k: {type: integer, default: 5}
      session: {type: string, note: "Filter by session name substring (--session)."}
      list: {type: boolean, note: "List all indexed sessions; no query (--list)."}
      json: {type: boolean, default: false}
    output:
      note: "Chunks with score, session_name, archived_date, chunk_index, source_file. Reranked: large score gap => relevance wins; near-tie => recency decides."
    constraints: {mode: read_only}

  semantic_search:
    path: tools/semantic_search_cli.py
    type: exec
    db: kb/vector_db
    purpose: "Semantic retrieval over kb corpus (hybrid BM25+RRF, cross-encoder + recency rerank, HyDE, date filter). Obscure theses / alpha."
    inputs:
      query: {type: string}
      mode: {type: enum, values: [search, list-recent], default: search}
      days-back: {type: integer, default: "7 (search) / 3 (list-recent)"}
      k: {type: integer, default: 5}
      max-chars: {type: integer, default: 1500, note: "0 = no limit"}
      json: {type: boolean, default: false}
    output:
      note: "Documents with content, score, rrf_score, metadata (source, title, date, tickers, tag, section_header, session, role...)."
    tuning:
      note: "Advanced BM25/HyDE/rerank toggles exist via env (SEM_BM25, SEM_RRF_K, SEM_HYDE, SEM_RERANK) + matching CLI flags. Defaults are tuned; only touch when a search is demonstrably off. HyDE needs DINOMEM_CHEAP_MODEL."
    constraints: {mode: read_only}

  exchange_data:
    path: tools/exchange_data.py
    type: exec
    purpose: "Multi-exchange + DEX price/OHLCV/orderbook/orderflow/funding/OI + flow-quality reads. CA auto-detect, retry/backoff. G1 no-TA (raw data only)."
    commands:
      # core data
      - price | ohlcv | orderbook | trades | advanced | funding | funding_history | oi | all
      - correlate | flow_stats | arbitrage | anomaly | unified
      # interpretive (LLM-facing narratives)
      - interpret_oi          # OI trend + OI-price divergence + funding-cross crowdedness, one-line 'overall'
      - interpret_depth       # ask-wall thickness, squeeze_days_of_volume, squeeze_potential, one-line 'overall'
      # flow-QUALITY axis (orthogonal to crowd-size — see caveats)
      - informed_flow         # INFORMED vs GAMBLE vs NOISE tier. Run BEFORE calling a crowded setup a fade (own block)
      - flow_persistence      # is aggregate flow convinced or flip-flopping? (own block)
      - distribution_watch    # accumulation->distribution TURN as it happens; sell-the-news exit confirm (own block)
      # risk / structural
      - regime_context        # G1-safe structural-risk Markov + vol-scaled SL anchor (own block). NOT directional
      # hyperliquid
      - hyperliquid_all | hyperliquid_meta | hyperliquid_hip3 | hyperliquid_book | hyperliquid_candles
      # perpdex adapters (dispatch via exchange_data)
      - perpdex_arb | perpdex_metrics | perpdex_resolve | perpdex_persist
    inputs:
      symbol: {type: string, required: true, note: "ticker | contract_address | coin_name. funding accepts ticker/coin; hyperliquid_book resolves HIP-3/live universe; hyperliquid_candles ticker-only; hyperliquid_meta/all/hip3 take none."}
      exchange: {type: enum, values: [binance, bybit, hyperliquid, all], default: all}
      dex: {type: boolean, default: false}
      chain: {type: string}
      interval: {type: string, default: "1h", note: "ohlcv, hyperliquid_candles, anomaly"}
      limit: {type: integer, default: 48}
      depth: {type: integer, default: 10, note: "orderbook"}
      target_price: {type: float, note: "orderbook / interpret_depth — measures liquidity to a level"}
      scan: {type: boolean, default: false, note: "anomaly"}
      threshold: {type: float, default: 0.1, note: "arbitrage"}
    key_reads:
      funding: "current_avg (cross-exchange), avg_30d, trend, extreme (>90th/<10th pctile), direction (crowded_longs|crowded_shorts), percentile. Aggregated Binance+Hyperliquid+OKX. avg_30d/percentile/extreme derived from REAL history, not a proxy."
      funding_history: "Raw per-exchange hourly funding series (NOT the aggregated snapshot). series.<ex>.data rows = [t,r,p] (t=epoch_ms, r=fundingRate, p=premium; okx adds rr=realizedRate), newest-first. --exchange hyperliquid|binance|okx|all, --limit N (hours back). For backfill, custom-window percentile recompute, regime-shift detection."
      interpret_depth: "wall_thickness, squeeze_days_of_volume (wall / avg daily vol), squeeze_potential, funding_signal. 'overall' = LLM one-liner."
      interpret_oi: "divergence (bullish_confirmation | bearish_divergence | weak_rally...), funding_cross (dangerous_longs | dangerous_shorts | neutral). 'overall' = LLM one-liner."
      anomaly: "score 0-1 (>=0.6 = anomaly), severity, flags, funding extreme. 0.4*short(6h)+0.6*long(48h)."
      regime_context: "suggested_sl_distance_pct = NOISE FLOOR (not a thesis stop), stationary.bear. risk-only, not directional."
    constraints: {mode: exec}

  # perpdex / regime_context are exposed to the LLM ONLY as exchange_data subcommands.
  #   perpdex_arb [SYM] | perpdex_metrics | perpdex_resolve SYM | perpdex_persist SYM [--exchange V] [--limit N]
  #     -> DEX funding-arb spreads + per-symbol funding persistence (persistent=True on BOTH legs = real carry).
  #        Caveat: APY = single-print annualization, NOT durable yield; ignores DEX exec/withdrawal cost. G1 no-TA.
  #   regime_context SYM -> structural-risk Markov + realized vol + vol-scaled SL floor + tail-heavy sizing read.
  #        NOT directional. Needs >=40 daily rows. G1 no-TA.

  informed_flow:
    invocation: "python3 tools/exchange_data.py informed_flow <SYMBOL> [--limit 500]"
    purpose: "INFORMED vs GAMBLE vs NOISE tier from CEX tape. Run BEFORE calling a crowded setup a fade."
    signed_factors: [lopsided_aggression, large_clip_discipline, oi_building, funding_earliness, relentless_flow_persistence, abnormal_vs_baseline]
    key_fields:
      tier: "likely_informed | leans_informed | ambiguous | leans_noise | likely_gamble_or_noise"
      crowded_reframe: "THE key field: 'CROWDING = CONFIRMATION here' (early+OI+aggr) vs 'CROWDING = FADE here' (late+extreme). Null if N/A."
      informed_score: ">=3 likely, >=1.5 leans, <=-1.5 gamble/noise"
      also: [flow_direction, aggression, imbalance_significant, flow_persistence, factors (audit trail), inputs]
    caveats:
      - "CROWDED (crowd-size) and INFORMED (flow-quality) are ORTHOGONAL. Do not collapse. Crowded+informed = confirmation; crowded+not-informed = fade; uncrowded+informed = early smart money."
      - "Reads informed SIZE not KNOWLEDGE. Small informed flow (an employee) is invisible in aggregate tape by construction. A disciplined gambler can mimic it."
      - "CEX tape is anonymized/aggregate only, never per-account. Per-account = on-chain (arkham_search)."
      - "Stack with catalyst TIMING (semantic_search) — informed accumulates BEFORE a dated public catalyst. G1 no-TA."
    constraints: {mode: read_only}

  flow_persistence:
    invocation: "python3 tools/exchange_data.py flow_persistence <SYMBOL> [--limit 500]"
    purpose: "Is aggregate flow convinced or flip-flopping?"
    key_fields:
      persistence: "fraction of slices agreeing with overall side (1.0 relentless, ~0.5 coin-flip)"
      flip_rate: "fraction of adjacent slices that changed side"
      read: "relentless_one_side | mostly_directional | mixed | indecisive_two_sided"
    caveats: ["AGGREGATE not per-account. High persistence = convinced net flow; high flip_rate = indecisive/gambling. Per-account conviction is on-chain only."]
    constraints: {mode: read_only}

  distribution_watch:
    invocation: "python3 tools/exchange_data.py distribution_watch <SYMBOL> [--limit 500]"
    purpose: "Catches accumulation->distribution TURN as it happens; sell-the-news exit confirm."
    key_fields:
      phase: "distribution_underway | new_short_positioning | accumulation_underway | short_covering | neutral"
      turn_confidence: "low | tentative | elevated (colored by flow persistence)"
      also: [flow_direction, oi_direction, oi_change_24h_pct, price_change_pct, signals, exit_note]
    caveats:
      - "COINCIDENT not predictive — confirms distribution is HAPPENING, does NOT forecast exit price/time."
      - "Sell-the-news exit timing needs the CATALYST CALENDAR (semantic_search/defillama), not tape alone. 'likely target' = magnet walls from interpret_depth."
    constraints: {mode: read_only}

  cohort_bias:
    invocation: "python3 tools/exchange_data.py cohort_bias <COIN> [--interval 24h|7d|30d]"
    purpose: "HyperTracker (CoinMarketMan) HL cohort intel: smart-vs-dumb PnL-cohort divergence. Clears the informed_flow per-account/skill blind spot with LABELED counterparties."
    sibling_cmds: [cohort_summary, cohort_heatmap, cohort_history]
    key_fields:
      divergence_score: "smart_bias - dumb_bias in [-1,1]. >0 winners more long (bullish edge); <0 winners more short (bearish edge). >=0.30 = strong."
      read: "strong|moderate_divergence_smart_<side> | consensus_<side> | no_clear_divergence | insufficient_cohort_data"
      edge: "bullish | bearish | neutral"
      smart_bias|dumb_bias: "fraction-long [0,1] for SMART[8,9] (Money Printer+Smart Money) vs DUMB[12,13,14,15] (Exit Liq+Semi/Full/Giga-Rekt). >0.55 long, <0.45 short."
      whale_bias_overlay: "WHALE[6,7] (Tidal+Leviathan) fraction-long. SIZE overlay ONLY, never enters divergence_score."
      also: [coin, cohorts_seen, smart_side, dumb_side, whale_side, overall]
    caveats:
      - "HL-only (Hyperliquid positions). Whale=SIZE axis, smart/dumb=SKILL axis; whale never scored into divergence."
      - "Strongest signal = winners and losers on OPPOSITE sides (labeled fade/confirmation gate). consensus_<side> = crowded, no edge."
      - "COINCIDENT positioning snapshot, not predictive. cohort_history (/position-metrics 15-min series) for trend; 24h/7d/30d windows only from 2026-03-19, all from 2026-02-19."
      - "Q1b: HT perpBias should agree directionally with HL OI/funding; flag divergence, don't trust blindly. Free tier 100 req/day. G1 no-TA."
    constraints: {mode: read_only}

  defillama_search:
    path: tools/defillama_search.py
    type: exec
    purpose: "Protocol TVL/revenue/fee trajectory, chain concentration, value-accrual score, token unlocks/emissions."
    inputs:
      protocol: {type: string, required: true, note: "Positional."}
      emissions: {type: flag, note: "--emissions <protocol> — vesting/unlock schedule."}
    output:
      note: "Formatted report: search results, TVL trends, revenue trajectory, fee summary, value_accrual_score, token unlocks (max_supply, next_unlock date/days_away/tokens/category, next 8 events)."
    constraints: {mode: read_only}

  arkham_search:
    path: tools/arkham_search.py
    type: exec
    purpose: "On-chain intel: whale/liquidation/entity tracking + PER-ACCOUNT holdings (the CEX-tape blind spot). Cookie auth (arkham_update_cookies.py refreshes)."
    subcommands:
      - "holders <chain> <ca> [--json]        # top holders w/ entity labels"
      - "holds <entity> <chain> <ca> [--json] # does entity hold token?"
      - "entitysearch <name...> [--json]      # fuzzy entity/address search"
      - "entity <entity_id> [--json]          # entity profile (id e.g. 'blackrock')"
      - "token <chain> <ca>                    # token metadata"
      - "address <addr> <chain>               # address owner/label"
      - "balances <addr> [chain]              # multichain portfolio"
      - "transfers <ca> <chain> [limit]       # recent transfer flow"
    also_insights: "Bare query (positional) + --tags/--tokens/--entities/--days/--limit searches Arkham insights (title, importance 1-10, tokens, entities, summary)."
    constraints: {mode: read_only}

  rugcheck_search:
    path: tools/rugcheck_search.py
    type: exec
    purpose: "Rug/honeypot/holder-concentration screen for microcap/degen DEX tokens. Free, keyless. Covers the two things pool-shape (exchange_data --dex) CANNOT see: contract rug vectors + insider supply concentration. EVM->GoPlus, SOL->RugCheck.xyz, holders->arkham. G1 no-TA."
    subcommands:
      - "rug <chain> <ca>       # FULL screen: contract rug vectors + holder concentration + combined overall"
      - "contract <chain> <ca>  # rug vectors only (GoPlus honeypot/tax/mint/LP-lock/creator% [EVM] | RugCheck score+risks [SOL])"
      - "holders <chain> <ca>   # unlabeled top-holder concentration only (nets out CEX/DEX/bridge/LP labels via arkham)"
    inputs:
      chain: {type: string, required: true, note: "ethereum|base|bsc|arbitrum|polygon|optimism|avalanche|robinhood (EVM) | solana (SOL). Positional."}
      ca: {type: string, required: true, note: "token contract address / SOL mint. Positional."}
      output: {type: string, note: "--output FILE writes JSON instead of stdout."}
    key_reads:
      rug: "overall (clean|elevated_risk|high_risk|inconclusive), flags[] (combined), contract{...}, holder_concentration{verdict,top5/top10_unlabeled_pct}."
      contract_evm: "is_honeypot, buy_tax/sell_tax, creator_percent, is_mintable, lp_locked_pct, flags[], severity (clean|soft|hard). hard = honeypot/cannot_sell."
      contract_sol: "score, rugged, risk_count, flags[] (RugCheck risk names+level), severity."
      holders: "verdict (distributed<35% | concentrated 35-60% | heavy >=60% top10 unlabeled), top5/top10_unlabeled_pct, unlabeled_wallets_seen."
    caveats:
      - "DEX/onchain tokens ONLY. CEX-listed tickers don't need this (they clear on CEX consensus)."
      - "Graceful degradation: a too-new token unindexed on GoPlus/RugCheck returns error in-band; holder-concentration can still succeed. overall=inconclusive only if BOTH contract AND holders error. Never crashes."
      - "arkham labels imperfect; a fresh launch with few CEX-labeled holders reads MORE concentrated by construction -> treat concentration as a FLAG not a verdict; heavy != automatic rug."
      - "severity 'hard' (honeypot/cannot-sell) is a real disqualifier; 'soft' (tax/mint/LP-unlock/concentration) is size-down risk, not a kill. G1 no-TA."
    constraints: {mode: read_only}

  consensus_search:
    path: tools/consensus_search.py
    type: exec
    purpose: "Crowd positioning + prediction-market consensus + Polymarket informed-flow (per-wallet). Two axes: crowd-SIZE and (--mode informed) flow-QUALITY."
    inputs:
      query: {type: string, required: true, note: "Positional."}
      limit: {type: integer, default: 3, note: "max Polymarket markets"}
      min_liquidity: {type: integer, default: 200000}
      min_bet: {type: integer, default: 500, note: "USD floor for trade-anomaly flagging"}
    modes:
      default: "Unified report: Polymarket markets, narrative consensus, exchange positioning, funding history, VIX, news."
      informed: "--mode informed on a DATABLE PUBLIC EVENT market: per-wallet on-chain read. tier, sniper_max_score (>=6 = coordinated), flagged/fresh wallets, market_prob_pct, time_to_resolution (= sell-the-news exit window)."
    caveats:
      - "Polymarket informed-flow is the CEX-tape twin: clears the per-account + exit-timing ceilings CEX tape cannot. Treat as probabilistic tilt, not proof; stack with exchange_data informed_flow."
      - "Known partial-failure history — mark reads inconclusive if a section errors, don't fabricate."
    constraints: {mode: exec}

  docs_search:
    path: tools/docs_search.py
    type: exec
    db: kb/vector_db_docs
    purpose: "Semantic search over long ingested docs (RAG). Filter by doc/section."
    inputs:
      query: {type: string, required: true}
      k: {type: integer, default: 5}
      doc: {type: string, note: "filter by title/filename substring"}
      section: {type: string, note: "filter by section title substring"}
      list: {type: boolean, note: "list indexed docs, no query"}
    output: {note: "Chunks with doc_title, section_title, chunk_index, score, content."}
    constraints: {mode: read_only}

  graph_search:
    path: tools/graph_search.py
    type: exec
    db: kb/memory_neuron/l2_graph/memory_graph.json
    purpose: "Relational recall: typed-edge BFS over the memory graph (depends_on / affects / trace). Fuzzy node match + similarity fallback."
    inputs:
      query: {type: string, required: true, note: "Positional. Fuzzy-matched vs node title/snippet (difflib >=0.60)."}
      hops: {type: integer, default: 2}
      min_confidence: {type: float, default: 0.6, note: "similarity edges require >=0.80"}
      output_format: {type: enum, values: [text, json], default: text}
    output: {note: "starting_node, hop_N nodes (relation label, confidence, snippet), forward_refs (terms in 2+ nodes but absent from graph)."}
    constraints: {mode: read_only}

<!-- BEGIN:dinomem-neuron (managed) -->
  session_search:
    path: tools/session_search.py
    type: exec
    db: kb/vector_db_sessions
    capabilities:
      - session_deep_recall
      - semantic_search_archived_sessions
      - recency_weighted_reranking
      - list_indexed_sessions
    scope: |
      Indexes only archived session .jsonl within the last 21 days (MAX_AGE_DAYS),
      ingested by procedures/session_ingest.py (cron via auto_session_reset.py,
      every 15 min). If the DB is missing, the tool errors with a build hint.
    inputs:
      query:
        type: string
        required: false
        note: "Positional. Required unless --list. Use natural language, not code identifiers."
      k:
        type: integer
        required: false
        default: 5
        note: "Number of results after recency-weighted rerank."
      session:
        type: string
        required: false
        note: "Filter by session name substring (--session)."
      list:
        type: boolean
        required: false
        note: "List all indexed sessions; no query needed (--list)."
      json:
        type: boolean
        required: false
        default: false
        note: "Output raw JSON instead of formatted text (--json)."
    output:
      type: list
      structure: chunks_with_metadata
      fields:
        - content: "Chunk text (consecutive user+assistant exchanges)."
        - score: "Relevance score (rounded). Reranked: large score gap => relevance wins; near-tie => recency decides."
        - session_name: "Source session identifier."
        - archived_date: "Date the session was archived (YYYY-MM-DD)."
        - chunk_index: "Position of chunk within the session."
        - source_file: "Archived .jsonl filename."
    constraints:
      mode: read_only
    libraries:
      - langchain_chroma
      - langchain_huggingface
      - sentence-transformers (multilingual-e5-small, local)

  docs_search:
    path: tools/docs_search.py
    type: exec
    db: kb/vector_db_docs
    capabilities:
      - semantic_search_long_docs
      - structure_aware_retrieval
      - filter_by_document
      - filter_by_section
      - list_indexed_documents
    note: errors with a build hint if kb/vector_db_docs is missing (run docs_ingest.py)
    inputs:
      query:
        type: string
        required: false
        note: "Positional. Required unless --list. Natural language."
      k:
        type: integer
        required: false
        default: 5
        note: "Number of results (--k)."
      doc:
        type: string
        required: false
        note: "Filter by document title/filename substring (--doc)."
      section:
        type: string
        required: false
        note: "Filter by section title substring (--section)."
      max_chars:
        type: integer
        required: false
        default: 1500
        note: "Truncate chunk content (--max-chars)."
      list:
        type: boolean
        required: false
        note: "List all indexed documents; no query needed (--list)."
      json:
        type: boolean
        required: false
        default: false
        note: "Output raw JSON instead of formatted text (--json)."
    output:
      type: list
      structure: chunks_with_metadata
      fields:
        - content: "Chunk text (section-aware)."
        - score: "Relevance score."
        - doc_title: "Source document title/filename."
        - section_title: "Section the chunk belongs to."
        - chunk_index: "Position of chunk within the document."
    constraints:
      mode: read_only
    libraries:
      - langchain_chroma
      - langchain_huggingface
      - sentence-transformers (multilingual-e5-small, local)

  data_query:
    path: tools/data_query.py
    type: exec
    db: kb/data_store/data.duckdb
    catalog: kb/data_store/catalog.json
    capabilities:
      - exact_sql_retrieval
      - csv_json_parquet_ingest
      - semi_structured_nested_json
      - filter_range_sort_aggregate
      - multi_table_one_db
      - list_catalog_tables
      - select_only_readonly
    inputs:
      sql:
        type: string
        required: false
        note: "SELECT-only query (--sql). Non-SELECT rejected."
      table:
        type: string
        required: false
        note: "Scope/inspect one catalog table (--table)."
      list:
        type: boolean
        required: false
        note: "List catalog tables+schema+keywords; no sql needed (--list)."
      format:
        type: string
        required: false
        default: text
        note: "text|json output (--format)."
    output:
      type: list
      structure: rows
    constraints:
      mode: read_only
    libraries:
      - duckdb

  data_register:
    path: procedures/data_register.py
    type: exec
    db: kb/data_store/data.duckdb
    catalog: kb/data_store/catalog.json
    capabilities:
      - materialize_csv_json_parquet
      - infer_column_types
      - write_catalog_entry
      - refresh_prune_hash_skip
      - nested_json
    inputs:
      file:
        type: string
        required: false
        note: "Source file to register (--file); csv|json|parquet."
      name:
        type: string
        required: false
        note: "Table name (--name); SQL-safe identifier."
      desc:
        type: string
        required: false
        note: "One-line dataset description (--desc)."
      keywords:
        type: string
        required: false
        note: "Comma-separated routing keywords (--keywords)."
      list:
        type: boolean
        required: false
        note: "List registered tables (--list)."
      dry_run:
        type: boolean
        required: false
        note: "Validate readability; no write (--dry-run)."
      refresh:
        type: boolean
        required: false
        note: "Re-scan sources; update schema/rows; prune vanished (--refresh)."
    output:
      type: text
    constraints:
      mode: read_write
    libraries:
      - duckdb

  code_graph:
    path: procedures/code_graph.py
    type: exec
    db: kb/code_graph/
    purpose: "ZERO-LLM code-structure indexer. tree-sitter AST -> nodes(file|function|class|method w/ file:line) + edges(calls|imports|inherits|contains, provenance extracted|inferred) into a SEPARATE code graph (reuses the graph_store SQLite backend, not the memory DB file). Build the structural index ONCE at $0, then code_query serves scoped subgraphs instead of grep+read whole files into context (~20x fewer tokens on big repos)."
    capabilities:
      - ast_extract_no_llm
      - path_and_line_nodes
      - extracted_inferred_provenance
      - git_incremental_rebuild
      - churn_node_weight
      - project_tag_and_cross_project_depends_on
    auto_ingest: "AUTO-REFRESHED daily by cron `code_graph_refresh` (T0 --command, 04:15, git-incremental so cheap) — mirrors docs_ingest. You normally do NOT run it by hand; it stays fresh. Manual run only to force-refresh mid-session before a code_query when you just edited source."
    project_tagging: "Every node carries a ZERO-LLM path-derived `project` tag (github/<name>/ -> <name>; else workspace basename). Boundary-crossing edges roll up into payload.project_edges (weighted depends_on, e.g. dinomem-neuron depends_on dinomem). Surfaced by code_query `projects` + `--project`."
    inputs:
      path:
        type: string
        required: false
        note: "Root to index (--path). Ladder: --path > DINOMEM_CODE_ROOTS env > agent workspace > ~/.openclaw."
      lang:
        type: string
        required: false
        note: "Restrict to one lang key, e.g. python (--lang). Default: all allowlisted extensions."
      full:
        type: boolean
        required: false
        note: "Force full rebuild (--full). Default: git-incremental when a prior DB+sha+cache exist (re-parses ONLY changed files; fail-open to full)."
    output:
      note: "Summary: mode (full|incremental|noop), node/edge counts, built_at_sha, parse_errors."
    constraints:
      mode: read_write
    libraries:
      - tree-sitter-language-pack (or tree-sitter + tree-sitter-languages)
    caveat: "OPTIONAL dep. Missing tree-sitter -> code_graph OFFLINE (fail-soft, rest of neuron unaffected). ZERO LLM in the build path by design."
    scope_decision: "CAPPED coverage: python/bash/json/markdown/JS/TS + go/rust/java/cpp/php/csharp/ruby/r/css/html/sql/yaml SHIPPED (18 exts, 17 lang keys). c# grammar pack key is 'csharp' NOT 'c_sharp' (the DownloadError was a wrong-name query, not a missing grammar). C-family (go/rust/java/cpp/php/csharp) + dynamic (ruby/r) share ONE generic walker (_cfamily_extract) driven by per-lang node-type SPECs (DRY); structural-only langs css(selectors+@import)/html(script|link src->imports)/sql(table|view defs+FROM|JOIN refs)/yaml(top-level keys, like json) have bespoke small walks. Same-name CROSS-FILE collision fixed: intra-file calls/inherits/references resolve to the SAME-FILE def first (subject AND target), before global last-wins name_to_idx. NB: adding a lang = extractor ONLY; ingestion (code_graph) + search (code_query/hybrid_recall CODE leg) are language-agnostic (traverse the graph, never re-parse) -> new lang works end-to-end automatically. JS/JSX/TS/TSX via one parse_javascript extractor; import/require + cross-file calls + class inherits, with tsconfig `paths` alias + workspace-glob resolution, barrel re-export hop (export */export{}from flattened importer->provider, cycle-guarded), module-scoped member calls (l.helper() resolves to the bound module's def, not a global same-name), AND cross-file named-call scoping (import {foo} from './m'; foo() resolves to m's foo, not a global same-name); external pkgs dropped, unscoped this./arr. member calls dropped; code_query explain annotates a flattened barrel edge with '(via barrel.ts)'). 40-lang parity is Graphify's PRODUCT need, NOT ours. Go/Rust/Java/C#/C++ + niche (fortran/ocaml/pascal/zig/etc) are ADD-ON-DEMAND: add a parse_<lang> + 2 lines (LANG_BY_EXT + PARSERS) ONLY when a real buyer file exists. Do NOT index the world. Extractors auto-honor the path-scoped exemption (memory/ docs/ data_query dirs excluded; env DINOMEM_CODE_GRAPH_EXEMPT adds more). Prompt-anchor sidecar (procedures/code_anchors.py -> code_query anchors) covers install.sh heredoc / cron-payload behavioral labels the AST can't see."

  code_query:
    path: tools/code_query.py
    type: exec
    db: kb/code_graph/code_graph.json
    purpose: "Query the code-structure graph. Ask for a SCOPED subgraph (what a symbol calls / is called by / inherits / lives in, or the edge path A->B) with file:line + EXTRACTED/INFERRED tags, instead of grep+read whole files. Reuses graph_search traversal (no fork)."
    subcommands:
      - "explain <symbol>   # neighborhood: contains/calls/called-by/imports/inherits, each file:line + provenance + «project» tag"
      - "path <A> <B>       # shortest edge path A -> B (how one symbol reaches another)"
      - "query \"<nl>\"       # fuzzy-find best-matching symbol then explain it"
      - "projects            # project inventory + cross-project depends_on rollup (which project's code calls/imports another's)"
    inputs:
      hops:
        type: integer
        required: false
        note: "Traversal depth for explain/query (--hops, default 1)."
      path_prefix:
        type: string
        required: false
        note: "Scope to a file-path prefix, e.g. procedures/ (--path)."
      project:
        type: string
        required: false
        note: "Scope explain/path/query to one project, e.g. --project dinomem-neuron (see the `projects` verb for names)."
      db:
        type: string
        required: false
        note: "Code graph json path (--db, default kb/code_graph/code_graph.json)."
    output:
      note: "Grouped edges w/ file:line + EXTRACTED/INFERRED tags. Prints a [stale] warning when built_at_sha != repo HEAD (refresh via code_graph.py). Also served fused via the hybrid_recall CODE leg for 'what does X do AND why'."
    constraints:
      mode: read_only
<!-- END:dinomem-neuron -->
