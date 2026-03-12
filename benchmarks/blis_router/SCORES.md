# BLIS Router Benchmark — Score Summary

## Scoring Formula

The evaluator uses an **improvement-based scoring formula** where each workload
contributes equally regardless of absolute latency scale.

```
Per workload:
  improvement_pct = (baseline_e2e - evolved_e2e) / baseline_e2e * 100

Overall:
  score = avg(improvement_pcts)
  regression_penalty = sum(excess * 3.0) for each workload regressing > 1%
    where excess = abs(improvement_pct) - 1.0
  combined_score = score - regression_penalty
```

Constants: `REGRESSION_TOLERANCE_PCT = 1.0`, `REGRESSION_PENALTY_RATE = 3.0`.

| Score | Meaning |
|-------|---------|
| 0.0 | Baseline — identical to initial router |
| > 0 (e.g. +7.6) | Better — 7.6% avg latency reduction across workloads |
| < 0 | Worse — regressions penalized proportionally |
| -100000 | Fatal error (build failure, all workloads failed) |

---

## Baseline (Initial Program)

The unoptimized router with fixed equal weights `[1.0, 1.0]` for prefix-affinity
and load-balance scorers. No adaptive logic, no request-aware routing.

**Source:** `openevolve_output/baseline_metrics.json` — identical in both
the original OpenEvolve project and the SkyDiscover port.

| Workload | Mean E2E (ms) | P95 E2E (ms) |
|----------|---------------|---------------|
| cache_warmup | 5734.57 | 10562.63 |
| load_spikes | 3667.04 | 8079.80 |
| multiturn | 731.70 | 1488.15 |
| **Average** | **3377.77** | **6710.19** |

Baseline `combined_score` = **0.0** (by definition — it's the reference point).

---

## OpenEvolve Baseline Run (Ground Truth)

**Source:** Original OpenEvolve project run at
`c:\vscode_projects\evolve_projects\openevolve\examples\blis_router\openevolve_output\`.

Run configuration:
- Model: `gcp/gemini-3-flash-preview` (via LiteLLM proxy)
- 100 iterations, 1 worker (sequential evaluation)
- 5 islands, population 100
- Log: `openevolve_output/logs/openevolve_20260311_154154.log`

### Best Result

| Metric | Value |
|--------|-------|
| **combined_score** | **+7.66** |
| avg_mean_improvement_pct | +7.66% |
| regression_count | 0 |
| Found at iteration | 57 (generation 2) |

| Workload | Baseline (ms) | Best Evolved (ms) | Change |
|----------|---------------|-------------------|--------|
| cache_warmup | 5734.57 | 4418.55 | **-22.9%** |
| load_spikes | 3667.04 | 3667.83 | +0.02% (flat) |
| multiturn | 731.70 | 731.43 | -0.04% (flat) |

### Evolution Progress

| Iteration | Score | Event |
|-----------|-------|-------|
| 0 | 0.00 | Baseline evaluated |
| 1-20 | all negative | No improvement found (regressions on load_spikes) |
| **21** | **+7.62** | First breakthrough — cache_warmup drops to 4424ms |
| 40 | +7.64 | Marginal improvement (+0.02) |
| **57** | **+7.66** | Final best — cache_warmup 4419ms, zero regressions |
| 58-100 | no improvement | Score plateaus, no further gains |

### Run Statistics

- Total evaluations: ~122 (100 iterations + initial + parallel batches)
- Build failures: 0
- Iterations with score > 0: ~47 (38%)
- Iterations at baseline (0.0): ~7 (6%)
- Iterations with regressions (< 0): ~68 (56%)
- Run duration: ~63 minutes (15:42 – 16:45)

### Key Observations

- **Only cache_warmup improved** (-22.9%). load_spikes and multiturn stayed flat.
  This matches the Oracle strategy pattern (see Fixed-Weight Strategies below).
- **Zero regressions** in the best solution — the penalty mechanism works.
- **First breakthrough at iteration 21** — the first 20 iterations all produced
  regressions, then the LLM found a cache-aware routing strategy.
- **Plateaued after iteration 57** — 43 more iterations produced no further gains,
  consistent with the ADR finding that the search space has a performance ceiling.

---

## SkyDiscover Results

### openevolve — 150 iterations (2026-03-12)

Run configuration:
- Model: `gcp/gemini-3-flash-preview` (via LiteLLM proxy)
- 150 iterations, 4 parallel workers
- 5 islands, population 40
- Log: `blis_router_results/openevolve_gemini-3-flash-preview/20260312_093935/`

#### Best Result

| Metric | Value |
|--------|-------|
| **combined_score** | **+8.83** |
| avg_mean_improvement_pct | +8.83% |
| regression_count | 0 |
| Found at iteration | 32 (generation 2) |

| Workload | Baseline (ms) | Best Evolved (ms) | vs Baseline | vs OE Ground Truth |
|----------|---------------|-------------------|-------------|---------------------|
| cache_warmup | 5734.57 | 4215.66 | **-26.5%** | -4.6% better (OE: 4418.55ms) |
| load_spikes | 3667.04 | 3669.37 | +0.06% (flat) | flat |
| multiturn | 731.70 | 731.31 | -0.05% (flat) | flat |

#### Evolution Progress

| Checkpoint | Score | Event |
|------------|-------|-------|
| 0 | 0.00 | Baseline evaluated |
| 1–10 | all ≤ 0 | No improvement — regressions only |
| ~13 | +3.36 | First positive score (with 1 regression, penalized) |
| **~15** | **+7.40** | First clean breakthrough — zero regressions |
| ~22 | +8.57 | Further improvement |
| **32** | **+8.83** | **Final best** — cache_warmup 4216ms, zero regressions |
| 33–150 | ≤ 8.83 | Score plateaus, no further gains |

#### Run Statistics

- Total evaluations: ~154 (150 iterations + initial + parallel overlap)
- Build failures: 0
- Run duration: ~21 minutes (09:39 – 10:00)

#### Comparison with OpenEvolve Ground Truth

| Metric | OE Ground Truth (100 iter) | SkyDiscover openevolve (150 iter) |
|--------|---------------------------|-----------------------------------|
| Best score | +7.66% | **+8.83%** |
| cache_warmup | 4418.55ms (-22.9%) | **4215.66ms (-26.5%)** |
| First breakthrough | Iteration 21 | Iteration ~15 |
| Plateau from | Iteration 57 | Iteration 32 |
| Workers | 1 (sequential) | 4 (parallel) |

**Conclusion:** SkyDiscover with 4 parallel workers finds a better solution faster.
Both runs show the same pattern: only cache_warmup improves, load_spikes and multiturn
stay flat — consistent with the Oracle strategy ceiling.

---

---

## Reference: Fixed-Weight Strategies

This section provides **best known hand-crafted results** for what routing weight strategies
can achieve on these workloads. The results were pre-computed by the original OpenEvolve
project using deterministic hand-crafted strategies (fixed simulator seed), not LLM evolution.

They serve two purposes:
1. **Port validation** — our Baseline row must match exactly, confirming the SkyDiscover
   port runs the same simulator with the same inputs.
2. **Performance reference** — the Oracle row shows the best known hand-crafted result,
   giving a reference point to compare evolved solutions against.

**Source:** `test_workloads/validation_results.json` from
[OpenEvolve blis branch](https://github.com/toslali-ibm/openevolve/tree/blis/examples/blis_router/test_workloads/validation_results.json),
commit `b859a64`.

| Strategy | Weights | cache_warmup (ms) | load_spikes (ms) | multiturn (ms) |
|----------|---------|-------------------|-------------------|----------------|
| **Baseline** | prefix=1, load=1 | 5734.57 | 3667.04 | 731.70 |
| Load-only | prefix=0, load=1 | 4237.92 | 3841.70 | 732.51 |
| Prefix-only | prefix=1, load=0 | 5734.57 | 9612.94 | 731.70 |
| Oracle | hand-crafted adaptive | 4151.21 | 3667.30 | 731.91 |
| Sabotaged | all → instance 0 | 19631.79 | 18592.29 | 3963.89 |

**Observations:**
- Oracle vs baseline: cache_warmup **-27.6%**, load_spikes ~flat, multiturn ~flat
- The main optimization opportunity is cache_warmup; other workloads are already near-optimal
  (Load-only drops cache_warmup to 4238ms at the cost of load_spikes +4.8%; Oracle achieves
  4151ms without regressing any workload — so the ceiling for cache_warmup improvement is ~27.6%
  and there is no headroom to improve load_spikes or multiturn without trade-offs)
- Our best evolved solution (4215ms) reaches **96% of Oracle's ceiling** (4151ms), leaving
  only ~1.5% of headroom — the search space is essentially exhausted at this point
