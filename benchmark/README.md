# dinomem benchmark suite

LongMemEval-style measurement harness for the dinomem memory system.
Purpose (per the Best-in-Class proposal): **build a benchmark BEFORE claiming
superiority.** Turns "I think it's better" into measured numbers:
false-promotion rate, multi-hop accuracy, stale-recall rate, latency/cost.

> **Two harnesses live here — pick by what you need:**
> - **`run_benchmark.py` (this one)** — fast, **free, no-LLM, retrieval-only** A/B
>   sanity check across 12 axes. Deterministic subprocess calls to the on-disk
>   tools (`hybrid_recall`/`graph_search`/`valid_time`). Answers *"did the right
>   memory surface and the wrong one stay buried?"* Run it for a cheap health check:
>   `python3 run_benchmark.py --all`.
> - **`run_all.py` (from base's full program)** — the **full multi-phase, LLM-in-
>   the-loop** evaluation (standard benchmarks + longitudinal/supersession/dedup/
>   pattern/promotion/behavior/poison/ablation → unified scorecard). Costs money
>   for the base/neuron arms; `--dry-run`/`--estimate-only` are free. Run it to
>   *prove* quality with citable numbers + a causal ablation table:
>   `python3 run_all.py --source . --dry-run`.
>
> Rule of thumb: **`run_benchmark.py` = cheap retrieval sanity; `run_all.py` = the
> full paid proof.** They're complementary, not duplicates.

## Layout
```
benchmark/
  run_benchmark.py        # runner: loads scenarios, runs A/B configs, scores, reports
  scenarios/*.jsonl       # one file per scenario type; each line = a graded case
  results/                # timestamped JSON + markdown reports
  README.md
```

## The 11 scenario types (matrix)
| id | name                | measures                                             |
|----|---------------------|------------------------------------------------------|
| 1  | temporal            | point-in-time / as-of correctness (valid_at/invalid_at) |
| 2  | contradiction       | conflicting facts resolved to the current truth      |
| 3  | multi_hop           | relational chains (A depends_on B affects C)         |
| 4  | entity_res          | aliases/canonical-id merge; no split-brain           |
| 5  | noise_resistance    | irrelevant bulk memory doesn't drown the answer      |
| 6  | longitudinal        | fact tracked across many updates over time           |
| 7  | false_promotion     | a single unconfirmed claim is NOT promoted to trusted|
| 8  | behavior            | preference/instruction recalled and applied          |
| 9  | recovery            | after a wrong/superseded memory, right answer returns|
| 10 | privacy_leak        | scope gate: peer/private facts don't leak to owner ctx|
| 11 | latency_cost        | retrieval latency + subprocess count budget          |

## Configs (A/B)
neuron = base + upgrade layer (a **superset**: a neuron install has everything
base has, plus the extra tools/crons, overwriting same-named files). So there is
no "neuron without graph/temporal" install — the honest comparison is two configs:
- `base`   : memory-only floor (plain lexical recall over memory/*.md, no neuron
             tools). What a base-only install can do.
- `neuron` : the full stack — hybrid_recall (memory+session+docs legs) + graph
             traversal + temporal (as_of) + scope gate. What the upgrade buys you.

The A/B delta = exactly the capability the neuron layer adds (temporal truth,
multi-hop, scope privacy) — the cases base structurally cannot answer.

## Run
```
python3 benchmark/run_benchmark.py --all
python3 benchmark/run_benchmark.py --scenario temporal --config neuron
python3 benchmark/run_benchmark.py --all --report md
```

## Scoring
Each case declares `expect` (substrings that MUST appear) and optionally
`forbid` (substrings that must NOT appear — the false-promotion / leak guards).
A case passes when all `expect` hit and no `forbid` hit. Latency/cost cases
score against a budget instead. Per-config pass-rate + per-scenario breakdown
land in results/.
