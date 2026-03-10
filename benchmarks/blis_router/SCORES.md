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
| > 0 (e.g. +15.0) | Better — 15% avg latency reduction across workloads |
| < 0 | Worse — regressions penalized proportionally |
| -100000 | Fatal error (build failure, all workloads failed) |

---

## Baseline (Initial Program)

The unoptimized router with fixed equal weights `[1.0, 1.0]` for prefix-affinity
and load-balance scorers. No adaptive logic, no request-aware routing.

| Workload | Mean E2E (ms) | P95 E2E (ms) |
|----------|---------------|---------------|
| cache_warmup | 5734.57 | 10562.63 |
| load_spikes | 3667.04 | 8079.80 |
| multiturn | 731.70 | 1488.15 |
| **Average** | **3377.77** | **6710.19** |

Baseline `combined_score` = **0.0** (by definition — it's the reference point).

---

## OpenEvolve Baseline Run

Running the original OpenEvolve project directly to establish a ground-truth
reference. Results will be added here once the run completes.

| Date | Model | Iterations | Best Score | cache_warmup | load_spikes | multiturn |
|------|-------|------------|------------|--------------|-------------|-----------|
| *(pending)* | | | | | | |

---

## SkyDiscover Results

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
