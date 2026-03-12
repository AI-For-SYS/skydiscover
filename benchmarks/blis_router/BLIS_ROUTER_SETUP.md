# BLIS Router Benchmark — SkyDiscover Port

## Overview

This benchmark was ported from [OpenEvolve's blis_router example](https://github.com/toslali-ibm/openevolve/tree/blis/examples/blis_router) to run on [SkyDiscover](https://github.com/skydiscover-ai/skydiscover).

**Goal:** Evolve adaptive routing logic for a BLIS multi-instance LLM inference cluster. The LLM modifies Go code that decides which GPU instance handles each request, trying to minimize end-to-end latency across 3 workload types.

**What gets evolved:** The `WeightedScoring.Route()` function in Go code, wrapped in `EVOLVE-BLOCK` markers inside a Python string (`initial_program.py`). The LLM sees the Go code and modifies the routing logic between the markers.

---

## Project Structure

```
benchmarks/blis_router/
  initial_program.py          # Go routing code in Python string (unchanged from OpenEvolve)
  evaluator.py                # Adapted for SkyDiscover API + Windows (see Changes below)
  hypothesis.py               # Hypothesis parsing/testing/ledger (unchanged from OpenEvolve)
  config.yaml                 # Adapted config (see Changes below)
  routing_policy.yaml         # BLIS simulator routing policy (unchanged from OpenEvolve)
  workload_v2_cache_warmup.yaml   # Workload definition (unchanged from OpenEvolve)
  workload_v2_load_spikes.yaml    # Workload definition (unchanged from OpenEvolve)
  workload_v2_multiturn.yaml      # Workload definition (unchanged from OpenEvolve)
  inference-sim/              # Go simulator (git clone, pinned to specific commit)
  output/                     # Auto-created at runtime
    baseline_metrics.json     #   Cached baseline run (delete to recompute)
    baseline.lock             #   FileLock for baseline race protection
    hypothesis_ledger.json    #   Accumulated hypothesis results
    slots/                    #   Parallel evaluation slot pool
      slot_0/ … slot_3/       #   Isolated inference-sim copies (lazily created)

scripts/
  run_blis_router.py          # Entry point script using SkyDiscover Python API
```

---

## Changes from OpenEvolve Original

The port preserves all evaluation logic, scoring formula, and system prompt identically.
Changes fall into two categories: **SkyDiscover API adaptation** and **Windows platform fixes**.

### evaluator.py — What Changed and Why

**Scoring logic: UNCHANGED.** The original improvement-based formula is preserved exactly:
```
score = avg(improvement_pcts) - regression_penalty
```
Where `improvement_pct` per workload = `(baseline - evolved) / baseline * 100` and
regressions are penalized with `penalty = sum(regression_pcts) * (regression_count / total_workloads)`.

| # | Change | Why | Logically equivalent? |
|---|--------|-----|----------------------|
| 1 | `+import platform` | Needed for Windows binary name detection | Yes — no-op on Linux |
| 2 | `-from openevolve.evaluation_result import EvaluationResult` | SkyDiscover doesn't use this class | Yes — see return type below |
| 3 | `+SIM_BINARY = "simulation_worker.exe" if platform.system() == "Windows" else "simulation_worker"` | Go produces `.exe` on Windows | Yes — same binary, platform-correct name |
| 4 | `"./simulation_worker"` → `str(slot_dir / SIM_BINARY)` | `./binary` doesn't work on Windows; slot_dir is the per-evaluation isolated copy | Yes — same binary, platform-correct path |
| 5 | `encoding="utf-8"` added to all `open()` calls | Windows defaults to cp1252, fails on UTF-8 chars (arrows, em dashes in Go comments) | Yes — Linux already uses UTF-8 by default |
| 6 | `"simulation_worker"` → `SIM_BINARY` in go build `-o` flag | Matches change #3 | Yes |
| 7 | `"openevolve_output"` → `"output"` in directory references | SkyDiscover uses its own output dir naming | Yes — just a directory name |
| 8 | `EvaluationResult(metrics={...}, artifacts={...})` → flat dict `{**metrics, "artifacts": artifacts}` | SkyDiscover evaluators return plain dicts, not framework objects | Yes — same data, different container |
| 9 | `evaluate()` return type annotation: `EvaluationResult` → `dict` | Matches change #8 | Yes |
| 10 | `result.metrics.get(...)` → `result.get(...)` in `__main__` block | Dict has no `.metrics` attribute — keys are top-level | Yes — same values accessed |
| 11 | `+import random, shutil` + `+from filelock import FileLock, Timeout` | Required for slot pool implementation | Yes — new imports only |
| 12 | `+MAX_PARALLEL_SLOTS = 4` + `+_ensure_slot_dir()` + `+_acquire_slot()` | Slot pool: isolated inference-sim copy per concurrent evaluate() call | Yes — same evaluation logic, now parallel-safe |
| 13 | `get_or_compute_baseline()`: wrapped slow path in `FileLock("baseline.lock", timeout=600)` with double-checked re-read inside | Prevents parallel evaluate() calls from racing to write routing.go / build in the original inference-sim dir | Yes — same baseline values, now race-free |
| 14 | `evaluate()`: acquires slot at entry, releases in `finally:` block | Ensures each concurrent call has its own routing.go + binary | Yes — same evaluation, now parallel-safe |

**NOT changed:** EVOLVE-BLOCK enforcement, workload execution, metric parsing, hypothesis testing, scoring formula, error handling, all thresholds and constants.

### config.yaml — What Changed and Why

| Setting | OpenEvolve | SkyDiscover | Why |
|---------|-----------|-------------|-----|
| Model config | `primary_model` / `secondary_model` | `llm.models` list | Different config schema — SkyDiscover uses a model pool |
| Diff mode | `diff_based_evolution: true` | `diff_based_generation: true` | Renamed key, same behavior |
| Full rewrites | `allow_full_rewrites: false` | *(removed)* | No SkyDiscover equivalent; EVOLVE-BLOCK enforcement is in the evaluator |
| Code length | `max_code_length: 50000` | `max_solution_length: 50000` | Renamed key, same value |
| Database section | `database: { population_size, num_islands, ... }` | *(removed)* | SkyDiscover search algorithms manage their own populations |
| Prompt settings | `num_top_programs: 3`, `num_diverse_programs: 2` | *(removed)* | Handled internally by each search algorithm |
| Eval parallelism | `parallel_evaluations: 1` | *(removed from config)* | Handled by slot pool in `evaluator.py` (see note below) |
| Eval timeout | `timeout: 60` | `timeout: 300` | Increased for Windows (Go builds slower, ~30s vs ~5s) |

**Preserved identically:** `system_message` (entire prompt), `max_iterations: 100`, `checkpoint_interval: 5`, `log_level: "INFO"`, `temperature: 0.7`, `top_p: null`, `max_tokens: 40000`, `timeout: 120`, `cascade_evaluation: false`.

**Parallel evaluation — slot pool:** The evaluator uses a pool of `MAX_PARALLEL_SLOTS = 4` isolated copies of the `inference-sim` source tree (`output/slots/slot_0/` … `slot_3/`). Each concurrent `evaluate()` call acquires a free slot via `FileLock`, writes its own `routing.go`, builds its own binary, and releases the slot when done. This matches the default framework concurrency of `max(max_parallel_iterations, 4) = 4`. If you raise `max_parallel_iterations` above 4, increase `MAX_PARALLEL_SLOTS` in `evaluator.py` accordingly.

### skydiscover/runner.py

- Lines 359, 372, 412: Added `encoding="utf-8"` to file open calls (same Windows fix as evaluator).

### skydiscover/extras/external/openevolve_backend.py

- `max_solution_length` → `max_code_length` mapping (without this, OpenEvolve used its default 10000, rejecting mutations for blis_router whose initial program is ~12635 chars).
- Best-program tracking fix: `controller.run()` sometimes returns a program with stale metrics. Now scans the full database for the true best `combined_score`.
- ~~`parallel_evaluations = 1`~~: removed — parallel evaluation is now handled correctly by the slot pool in `evaluator.py`.

---

## How the Evaluator Works

The evaluator supports parallel execution via a **slot pool**: `MAX_PARALLEL_SLOTS = 4`
isolated copies of the `inference-sim` source tree under `output/slots/slot_0/` … `slot_3/`.
Each concurrent `evaluate()` call acquires a free slot using `FileLock`, works entirely
within that slot's directory, and releases the lock when done. This prevents concurrent
builds from corrupting each other's `routing.go` or binary. Slots are created lazily on
first use by copying the source tree (excluding binaries and `.git`).

Each evaluation cycle:

1. **Acquire slot** — scan slots non-blocking for a free one; if none, block on a random
   slot with a 450s timeout (worst case: build ~60s + 3 × 120s workloads = ~420s + margin)
2. **Extract Go code** from the Python wrapper (`GO_ROUTING_CODE = """..."""`)
3. **Enforce EVOLVE-BLOCK boundary** — splice only the evolved block into the template, revert any out-of-block changes
4. **Get baseline** — read from `output/baseline_metrics.json` cache; compute and cache on first call (protected by `baseline.lock`)
5. **Parse hypotheses** — extract any `// HYPOTHESIS-N` / `// EXPECT-N` comments from the Go code
6. **Write** the Go code to `output/slots/slot_N/sim/routing.go`
7. **Build** via `go build -o <SIM_BINARY> main.go` inside the slot directory
8. **Run 3 workloads** (cache_warmup, load_spikes, multiturn) — each is a separate subprocess call
9. **Parse cluster metrics** from JSON output (e2e_mean_ms, e2e_p95_ms, completed_requests)
10. **Compute score** using improvement-based formula (see Scoring below)
11. **Test hypotheses** against actual results; update `output/hypothesis_ledger.json`
12. **Release slot** and return dict with `combined_score` and metrics

**Baseline:** Computed once on first run, protected by its own `FileLock("baseline.lock")`
to avoid duplicate computation under parallelism, and cached to `output/baseline_metrics.json`.
Delete this file to recompute.

### Scoring

The scoring formula (identical to OpenEvolve original):

```
Per workload:
  improvement_pct = (baseline_e2e - evolved_e2e) / baseline_e2e * 100

Overall:
  avg_improvement = mean(improvement_pcts across all successful workloads)
  regression_penalty = sum((abs(improvement_pct) - 1.0) * 3.0)
                       for each workload where improvement_pct < -1.0%
  combined_score = avg_improvement - regression_penalty
```

Constants: `REGRESSION_TOLERANCE_PCT = 1.0` (1% tolerance before penalty), `REGRESSION_PENALTY_RATE = 3.0` (penalty multiplier per excess percentage point).

| Score | Meaning |
|-------|---------|
| 0.0 | Baseline (no change from initial router) |
| > 0 (e.g. +15.0) | Better than baseline — 15% average latency reduction |
| < 0 (e.g. -822.0) | Worse than baseline — regressions penalized |
| -100000 | Fatal error (build failure, all workloads failed) |

---

## Setup

### Prerequisites
- Python >= 3.10 with `uv`
- Go compiler (tested with go1.26.1)
- `OPENAI_API_KEY` environment variable set (for LiteLLM proxy)
- OpenEvolve package installed (`uv pip install openevolve` for the `openevolve` backend)

### inference-sim Setup

**Critical:** Must be pinned to commit `7b59c19` (the OpenEvolve submodule pin). Latest breaks: removed `hash.HashTokens`, changed struct fields.

```bash
cd benchmarks/blis_router
git clone https://github.com/inference-sim/inference-sim
cd inference-sim
git checkout 7b59c1966364ef4a5a0c100ada44bcad3e2d0853
go build -o simulation_worker.exe main.go   # Windows
# go build -o simulation_worker main.go     # Linux/Mac
```

### Running

```bash
# Quick test (2 iterations)
uv run python scripts/run_blis_router.py

# OpenEvolve backend, 10 iterations
uv run python scripts/run_blis_router.py --search openevolve --iterations 10

# Full run with specific algorithm
uv run python scripts/run_blis_router.py --search adaevolve --iterations 50

# All CLI options
uv run python scripts/run_blis_router.py --search <algo> --model <model> --iterations <n> --api-base <url>
```

### Output

Results go to `blis_router_results/{algorithm}_{model}/{timestamp}/`:
- `best/best_program.py` — best evolved routing code
- `best/best_program_info.json` — metrics of the best program
- `checkpoints/` — periodic snapshots (every 5 iterations)
- `logs/` — detailed run logs
- Scores are appended to `blis_router_results/scores.jsonl`

### Available search algorithms

| Algorithm | Type | Description |
|-----------|------|-------------|
| `adaevolve` | Built-in | Multi-island adaptive search with UCB, migration, breakthroughs |
| `openevolve_native` | Built-in | MAP-Elites + island-based evolutionary search (SkyDiscover reimplementation) |
| `gepa_native` | Built-in | Pareto-efficient search with reflective prompting + LLM merge |
| `evox` | Built-in | Self-evolving paradigm with co-adaptive experience management |
| `topk` | Built-in | Top-K selection and refinement |
| `beam_search` | Built-in | Breadth-first beam expansion |
| `best_of_n` | Built-in | Generate N variants, keep the best |
| `openevolve` | External | Wraps the [OpenEvolve](https://github.com/codelion/openevolve) package |

---

## LLM Provider Configuration

Current setup uses a LiteLLM proxy. The `api_base` must be passed explicitly in `run_discovery()` because SkyDiscover's provider lookup may ignore the config value.

```python
result = run_discovery(
    ...
    model="gcp/gemini-3-flash-preview",
    api_base="https://ete-litellm.ai-models.vpc-int.res.ibm.com",  # MUST pass explicitly
    ...
)
```

API key: resolved from `OPENAI_API_KEY` environment variable.

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `undefined: hash.HashTokens` | Wrong inference-sim commit | `git checkout 7b59c19` in inference-sim/ |
| `snap.InFlightRequests undefined` | Wrong inference-sim commit | Same as above |
| `charmap codec can't decode` | Missing UTF-8 encoding | Ensure `encoding="utf-8"` on all `open()` calls |
| `[WinError 2] file not found` | Windows path issue | Use absolute path: `str(inference_sim_dir / SIM_BINARY)` |
| Score always -100000 | Build or workload failure | Check logs for Go syntax errors or missing files |
| Stale baseline results | Cached baseline | Delete `output/baseline_metrics.json` to recompute |
| `file being used by another process` | Slot pool exhausted or stale lock | Delete `output/slots/` to reset; check `MAX_PARALLEL_SLOTS` in `evaluator.py` |
