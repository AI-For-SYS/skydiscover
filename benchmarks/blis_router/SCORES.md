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

All results compared against the OpenEvolve baseline run above (same model, same evaluator).

| Date | Algorithm | Backend | Model | Iterations | Best Score | Notes |
|------|-----------|---------|-------|------------|------------|-------|
| *(no valid runs yet)* | | | | | | |

---

## Reference: Fixed-Weight Strategies

**Source:** `test_workloads/validation_results.json` from
[OpenEvolve blis branch](https://github.com/toslali-ibm/openevolve/tree/blis/examples/blis_router/test_workloads/validation_results.json),
commit `b859a64`.

Deterministic (same simulator seed). Our baseline matches these numbers exactly,
confirming the port is faithful.

| Strategy | Weights | cache_warmup (ms) | load_spikes (ms) | multiturn (ms) |
|----------|---------|-------------------|-------------------|----------------|
| **Baseline** | prefix=1, load=1 | 5734.57 | 3667.04 | 731.70 |
| Load-only | prefix=0, load=1 | 4237.92 | 3841.70 | 732.51 |
| Prefix-only | prefix=1, load=0 | 5734.57 | 9612.94 | 731.70 |
| Oracle | hand-crafted adaptive | 4151.21 | 3667.30 | 731.91 |
| Sabotaged | all → instance 0 | 19631.79 | 18592.29 | 3963.89 |

**Observations:**
- Oracle vs baseline: cache_warmup **-27.6%**, load_spikes ~flat, multiturn ~flat
- The main optimization opportunity is cache_warmup; other workloads are near-optimal
- The OpenEvolve run achieved -22.9% on cache_warmup, reaching 83% of Oracle's -27.6%
