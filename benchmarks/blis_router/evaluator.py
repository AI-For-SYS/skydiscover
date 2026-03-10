"""
BLIS Router Evaluator

Evaluates evolved routing algorithms by:
1. Extracting Go code from Python wrapper
2. Writing evolved routing.go to BLIS source
3. Building BLIS
4. Running simulations on 3 routing-sensitive v2 workloads
5. Testing inline hypotheses against baseline (cached after first eval)
6. Computing normalized per-workload improvement score

Score = average improvement percentage across all workloads (mean latency).
Each workload contributes equally. Regressing any workload >1% incurs a penalty.
Positive score = better than baseline. 0 = same as baseline.
"""

import json
import logging
import os
import re
import subprocess
import traceback
from difflib import unified_diff
from pathlib import Path

from openevolve.evaluation_result import EvaluationResult
from hypothesis import (
    parse_hypotheses,
    test_hypotheses,
    load_ledger,
    update_ledger,
    generate_knowledge_base_summary,
    format_hypothesis_results,
)

# Use logging instead of print() so output is captured in worker processes
logger = logging.getLogger(__name__)

# V2 workloads: longer durations, validated routing-sensitive.
# Shared between get_or_compute_baseline() and evaluate().
WORKLOADS = [
    ("cache_warmup", "workload_v2_cache_warmup.yaml"),
    ("load_spikes", "workload_v2_load_spikes.yaml"),
    ("multiturn", "workload_v2_multiturn.yaml"),
]


SIM_MODEL = os.environ.get("BLIS_MODEL", "meta-llama/llama-3.1-8b-instruct")


def _build_sim_cmd(
    inference_sim_dir: Path, policy_config_path: Path, workload_path: Path
) -> list[str]:
    """Return the simulation command list for a single workload run."""
    cmd = [
        "./simulation_worker",
        "run",
        "--model",
        SIM_MODEL,
        "--num-instances",
        "4",
        "--policy-config",
        str(policy_config_path),
        "--workload-spec",
        str(workload_path),
        "--log",
        "info",
    ]
    # Qwen needs blackbox coefficients; other models use simulator defaults
    if "Qwen" in SIM_MODEL:
        cmd += [
            "--hardware",
            "H100",
            "--tp",
            "1",
            "--alpha-coeffs",
            "4680.303204056608,0.0,0.0",
            "--beta-coeffs",
            "7051.796874715078,19.538416565504026,25.431830886933543",
            "--total-kv-blocks",
            "65833",
            "--max-num-running-reqs",
            "256",
            "--max-num-scheduled-tokens",
            "4096",
        ]
    return cmd


def extract_evolve_block(code: str) -> str:
    """Extract only EVOLVE-BLOCK section from Go code."""
    pattern = r"// EVOLVE-BLOCK-START(.*?)// EVOLVE-BLOCK-END"
    match = re.search(pattern, code, re.DOTALL)
    return match.group(1).strip() if match else ""


def _extract_go_imports(code: str) -> str:
    """Extract the import block from Go code, e.g. import (\n\t"fmt"\n)."""
    match = re.search(r'import \(\n(.*?)\)', code, re.DOTALL)
    return match.group(1) if match else ""


def enforce_evolve_block_boundary(evolved_code: str, template_code: str) -> str:
    """Splice the EVOLVE-BLOCK and imports from evolved code into the template.

    The LLM sometimes generates diffs that modify struct definitions or other code
    outside the EVOLVE-BLOCK, causing build failures. This function takes:
      - The EVOLVE-BLOCK from the evolved code (the creative part)
      - The import block from the evolved code (LLM may need math, sort, etc.)
      - Everything else from the initial program template (known-good)

    Returns the spliced code, or the original evolved_code if markers are missing.
    """
    evolved_block = extract_evolve_block(evolved_code)
    if not evolved_block:
        return evolved_code  # No markers found — pass through unchanged

    # Replace the EVOLVE-BLOCK in the template with the evolved version
    block_pattern = r"(// EVOLVE-BLOCK-START\n)(.*?)([ \t]*// EVOLVE-BLOCK-END)"
    template_match = re.search(block_pattern, template_code, re.DOTALL)
    if not template_match:
        return evolved_code  # Template has no markers — pass through

    spliced = (
        template_code[: template_match.start()]
        + template_match.group(1)
        + evolved_block
        + "\n\t"
        + template_match.group(3)
        + template_code[template_match.end() :]
    )

    # Also carry over evolved imports (LLM may add "math", "sort", etc.)
    evolved_imports = _extract_go_imports(evolved_code)
    template_imports = _extract_go_imports(spliced)
    if evolved_imports and template_imports and evolved_imports != template_imports:
        spliced = spliced.replace(
            f"import (\n{template_imports})",
            f"import (\n{evolved_imports})",
            1,
        )

    return spliced


def print_diff(initial_code: str, current_code: str):
    """Print compact colored diff between initial and current EVOLVE-BLOCK."""
    initial_block = extract_evolve_block(initial_code)
    current_block = extract_evolve_block(current_code)

    if not initial_block or not current_block:
        return

    initial_lines = initial_block.splitlines(keepends=True)
    current_lines = current_block.splitlines(keepends=True)

    diff = list(unified_diff(initial_lines, current_lines, lineterm=""))
    if not diff:
        logger.info("!!!! NO DIFF FOUND - code unchanged from initial")
        return  # No changes

    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))

    logger.info(f"Diff vs initial: -{removed} / +{added} lines")


def _parse_cluster_metrics(output_text: str) -> dict | None:
    """Parse cluster-wide metrics from simulation output JSON blocks."""
    json_blocks = []
    in_json = False
    json_buffer = ""
    brace_count = 0
    for line in output_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{"):
            in_json = True
            brace_count = 0
        if in_json:
            json_buffer += line + "\n"
            brace_count += stripped.count("{") - stripped.count("}")
            if brace_count == 0 and json_buffer.strip():
                try:
                    json_blocks.append(json.loads(json_buffer))
                except json.JSONDecodeError:
                    pass
                json_buffer = ""
                in_json = False
    for block in json_blocks:
        if block.get("instance_id") == "cluster":
            return block
    return None


def get_or_compute_baseline(
    script_dir: Path, inference_sim_dir: Path, policy_config_path: Path
) -> dict:
    """Get baseline metrics from cache or compute by running the initial program.

    On first call, writes the initial program's Go code to routing.go, builds,
    runs all 5 workloads, extracts per-workload metrics, computes aggregate
    scores, and caches to baseline_metrics.json.  Subsequent calls read from
    cache.

    Returns:
        dict with per-workload e2e_ms keys plus avg_e2e_ms, avg_p95_ms,
        combined_score.  Returns empty dict on failure.
    """
    output_dir = script_dir / "openevolve_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "baseline_metrics.json"
    if cache_path.exists():
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read baseline cache, recomputing: %s", exc)

    # Load initial program
    initial_program_path = script_dir / "initial_program.py"
    if not initial_program_path.exists():
        logger.warning("initial_program.py not found; cannot compute baseline")
        return {}

    with open(initial_program_path, "r") as f:
        initial_text = f.read()

    go_code = extract_go_code(initial_text)
    if not go_code:
        logger.warning("Could not extract Go code from initial program for baseline")
        return {}

    # Write initial routing.go
    routing_go_path = inference_sim_dir / "sim" / "routing.go"
    try:
        with open(routing_go_path, "w") as f:
            f.write(go_code)
    except OSError as exc:
        logger.warning("Failed to write routing.go for baseline: %s", exc)
        return {}

    # Build
    try:
        build_result = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=inference_sim_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if build_result.returncode != 0:
            logger.warning("Baseline build failed: %s", build_result.stderr[:300])
            return {}
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Baseline build error: %s", exc)
        return {}

    # Run all workloads
    baseline = {}
    latencies = []
    tail_latencies = []

    for workload_name, workload_file in WORKLOADS:
        workload_path = script_dir / workload_file
        cmd = _build_sim_cmd(inference_sim_dir, policy_config_path, workload_path)
        try:
            sim_result = subprocess.run(
                cmd,
                cwd=inference_sim_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if sim_result.returncode != 0:
                logger.warning("Baseline %s failed: %s", workload_name, sim_result.stderr[:300])
                continue
            output_text = sim_result.stdout + (sim_result.stderr or "")
            cluster_metrics = _parse_cluster_metrics(output_text)
            if cluster_metrics and "e2e_mean_ms" in cluster_metrics:
                e2e_ms = float(cluster_metrics["e2e_mean_ms"])
                e2e_p95_ms = float(cluster_metrics.get("e2e_p95_ms", e2e_ms))
                baseline[f"{workload_name}_e2e_ms"] = e2e_ms
                latencies.append(e2e_ms)
                tail_latencies.append(e2e_p95_ms)
            else:
                logger.warning("Baseline %s: no cluster metrics found", workload_name)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Baseline %s error: %s", workload_name, exc)

    if latencies:
        avg_e2e = sum(latencies) / len(latencies)
        avg_p95 = sum(tail_latencies) / len(tail_latencies)
        baseline["avg_e2e_ms"] = avg_e2e
        baseline["avg_p95_ms"] = avg_p95
        baseline["combined_score"] = 0.0  # Baseline is the reference point

    # Cache
    try:
        with open(cache_path, "w") as f:
            json.dump(baseline, f, indent=2)
        logger.info("Cached baseline metrics to %s", cache_path)
    except OSError as exc:
        logger.warning("Failed to cache baseline metrics: %s", exc)

    return baseline


def extract_go_code(program_text: str) -> str:
    """Extract Go code from Python program text"""

    # Look for GO_ROUTING_CODE variable
    match = re.search(r'GO_ROUTING_CODE\s*=\s*"""(.*?)"""', program_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If no Python wrapper, assume it's raw Go code
    if "package sim" in program_text:
        return program_text

    return ""


def evaluate(program_path: str) -> EvaluationResult:
    """
    Evaluate the evolved routing algorithm.

    Args:
        program_path: Path to the program file containing routing.go code

    Returns:
        EvaluationResult with metrics and artifacts
    """

    # Load program text from file
    with open(program_path, "r") as f:
        program_text = f.read()

    # Get paths
    script_dir = Path(__file__).parent
    inference_sim_dir = script_dir / "inference-sim"
    routing_go_path = inference_sim_dir / "sim" / "routing.go"
    policy_config_path = script_dir / "routing_policy.yaml"

    # Step 1: Extract Go code from Python wrapper
    logger.info(f"Program text preview: {program_text[:100]}...")
    go_code = extract_go_code(program_text)
    if not go_code:
        logger.error("Failed to extract Go code from program")
        logger.error(f"Program text length: {len(program_text)}")
        logger.error(f"Contains GO_ROUTING_CODE: {'GO_ROUTING_CODE' in program_text}")
        return EvaluationResult(
            metrics={
                "combined_score": -100000.0,
                "avg_e2e_ms": float("inf"),
                "error": "Failed to extract Go code",
            },
            artifacts={
                "error_type": "ExtractionError",
                "error_message": "Could not find GO_ROUTING_CODE variable or valid Go code",
                "suggestion": 'Ensure program contains GO_ROUTING_CODE = """...""" or starts with \'package sim\'',
            },
        )

    logger.info(f"Extracted Go code: {len(go_code)} chars, first line: {go_code.split(chr(10))[0]}")

    # Enforce EVOLVE-BLOCK boundary: only take the evolved block, keep the rest
    # from the initial program template. This prevents LLM diffs that modify
    # imports, struct definitions, or other code outside the EVOLVE-BLOCK.
    initial_program_path = script_dir / "initial_program.py"
    if initial_program_path.exists():
        with open(initial_program_path, "r") as f:
            template_go_code = extract_go_code(f.read())
        if template_go_code:
            go_code_before = go_code
            go_code = enforce_evolve_block_boundary(go_code, template_go_code)
            if go_code != go_code_before:
                logger.info("Enforced EVOLVE-BLOCK boundary (reverted out-of-block changes)")

    # Show diff vs initial program if enabled
    show_diffs = os.environ.get("OPENEVOLVE_SHOW_DIFFS", "true").lower() == "true"
    if show_diffs or True:

        try:
            initial_program_path = script_dir / "initial_program.py"
            if initial_program_path.exists():
                with open(initial_program_path, "r") as f:
                    initial_text = f.read()
                initial_go_code = extract_go_code(initial_text)
                if initial_go_code:
                    print_diff(initial_go_code, go_code)
        except Exception as e:
            pass  # Silently skip if diff fails

    # On the very first evaluation, get_or_compute_baseline writes the initial
    # routing.go, builds, and runs all workloads to populate the cache.  The
    # evolved routing.go is then written and built again in Steps 2-3 below,
    # resulting in a double-build on the first call only.  Subsequent evals skip
    # straight to the cached baseline so no extra build occurs.
    baseline_metrics = get_or_compute_baseline(script_dir, inference_sim_dir, policy_config_path)
    hypotheses = parse_hypotheses(go_code)

    if hypotheses:
        logger.info(f"Parsed {len(hypotheses)} hypotheses from evolved code")
        for h in hypotheses:
            logger.info(f"  H{h['id']}: {h['claim']} (EXPECT: {h['metric']} < {h['threshold']})")
    else:
        logger.info("No hypotheses found in evolved code")

    # Step 2: Write evolved routing.go
    try:
        with open(routing_go_path, "w") as f:
            f.write(go_code)
        logger.info(f"Wrote evolved routing.go to {routing_go_path}")
    except Exception as e:
        logger.error(f"Failed to write routing.go: {e}")
        logger.error(traceback.format_exc())

        return EvaluationResult(
            metrics={
                "combined_score": -100000.0,  # Very bad score for file write failure
                "avg_e2e_ms": float("inf"),
                "error": f"Failed to write file: {e}",
            },
            artifacts={
                "error_type": "FileWriteError",
                "error_message": str(e),
                "full_traceback": traceback.format_exc(),
            },
        )

    # Step 3: Build BLIS
    try:
        logger.info("Building BLIS...")
        result = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=inference_sim_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            logger.error(f"Build failed: {result.stderr}")

            # Truncate error for metrics (keep full version in artifacts)
            error_summary = result.stderr.strip()[:500] if result.stderr else "Unknown build error"

            return EvaluationResult(
                metrics={
                    "combined_score": -100000.0,  # Very bad score for build failure
                    "avg_e2e_ms": float("inf"),
                    "error": f"Build failed: {error_summary}",
                },
                artifacts={
                    "error_type": "BuildError",
                    "error_message": "Go build failed - likely syntax error in evolved code",
                    "build_stderr": result.stderr,
                    "suggestion": "Check for Go syntax errors in the evolved EVOLVE-BLOCK section",
                },
            )

        logger.info("Build successful")
    except subprocess.TimeoutExpired:
        logger.error("Build timed out")

        return EvaluationResult(
            metrics={
                "combined_score": -100000.0,
                "avg_e2e_ms": float("inf"),
                "error": "Build timeout",
            },
            artifacts={
                "error_type": "BuildTimeout",
                "error_message": "Go build exceeded 60 second timeout",
                "suggestion": "Build should be fast - this indicates a serious problem",
            },
        )
    except Exception as e:
        logger.error(f"Build error: {e}")
        logger.error(traceback.format_exc())

        return EvaluationResult(
            metrics={
                "combined_score": -100000.0,
                "avg_e2e_ms": float("inf"),
                "error": f"Build error: {e}",
            },
            artifacts={
                "error_type": type(e).__name__,
                "error_message": str(e),
                "full_traceback": traceback.format_exc(),
            },
        )

    # Step 4: Run simulations on 3 routing-sensitive v2 workloads
    # Validated: sabotaged (always-instance-0) is 194-416% worse than baseline.
    # - cache_warmup: 3 prefix groups + no-prefix, rate=1000, 5s sim (load-aware wins)
    # - load_spikes: heavy-hitter prefix + realtime + light prefix, rate=1000, 5s sim (prefix-only = +113%)
    # - multiturn: multi-turn sessions (prefix=4096/2048/1024) + realtime, rate=150, 10s sim (load-only = +5.5%)
    latencies = []
    tail_latencies = []  # p99 latencies
    request_counts = []  # for weighted averaging
    workload_results = {}
    failed_workloads = []

    for workload_name, workload_file in WORKLOADS:
        try:
            logger.info(f"Running {workload_name} workload...")

            # Workload file path (relative to script directory)
            workload_path = script_dir / workload_file

            cmd = _build_sim_cmd(inference_sim_dir, policy_config_path, workload_path)

            result = subprocess.run(
                cmd, cwd=inference_sim_dir, capture_output=True, text=True, timeout=120
            )

            if result.returncode != 0:
                logger.error(f"{workload_name} workload failed: {result.stderr}")
                failed_workloads.append(workload_name)
                workload_results[workload_name] = {
                    "e2e_ms": None,
                    "error": "Simulation failed",
                    "stderr": result.stderr[:500],
                }
                continue

            # Parse JSON output - BLIS outputs multiple JSON blocks with headers
            # Format: "=== Simulation Metrics ===" followed by JSON object
            # We want the CLUSTER aggregate (instance_id == "cluster")
            try:
                # Find all JSON blocks in the output (could be in stdout or stderr)
                output_text = result.stdout + (result.stderr or "")
                cluster_metrics = _parse_cluster_metrics(output_text)

                if cluster_metrics and "e2e_mean_ms" in cluster_metrics:
                    e2e_ms = float(cluster_metrics["e2e_mean_ms"])
                    e2e_p95_ms = float(cluster_metrics.get("e2e_p95_ms", e2e_ms))
                    latencies.append(e2e_ms)
                    tail_latencies.append(e2e_p95_ms)
                    # Get request count from simulation output (fallback to 1 for equal weight)
                    num_requests = cluster_metrics.get("completed_requests", 1)
                    request_counts.append(int(num_requests))
                    workload_results[workload_name] = {
                        "e2e_ms": e2e_ms,
                        "e2e_p95_ms": e2e_p95_ms,
                        "completed_requests": num_requests,
                        "ttft_mean_ms": cluster_metrics.get("ttft_mean_ms"),
                        "itl_mean_ms": cluster_metrics.get("itl_mean_ms"),
                        "tokens_per_sec": cluster_metrics.get("tokens_per_sec"),
                    }
                    logger.info(f"{workload_name}: e2e_mean={e2e_ms:.2f}ms, p95={e2e_p95_ms:.2f}ms")
                else:
                    logger.error(f"Could not find cluster metrics in {workload_name} output")
                    failed_workloads.append(workload_name)
                    workload_results[workload_name] = {
                        "e2e_ms": None,
                        "error": "Failed to find cluster metrics",
                    }
            except Exception as parse_error:
                logger.error(f"Error parsing {workload_name} output: {parse_error}")
                logger.error(f"Output sample: {output_text[:500]}")
                failed_workloads.append(workload_name)
                workload_results[workload_name] = {
                    "e2e_ms": None,
                    "error": f"Parse error: {str(parse_error)}",
                    "output_sample": output_text[:500],
                }

        except subprocess.TimeoutExpired:
            logger.error(f"{workload_name} workload timed out")
            failed_workloads.append(workload_name)
            workload_results[workload_name] = {"e2e_ms": None, "error": "Timeout (120s)"}
        except Exception as e:
            logger.error(f"{workload_name} workload error: {e}")
            failed_workloads.append(workload_name)
            workload_results[workload_name] = {"e2e_ms": None, "error": str(e)}

    # Step 5: Compute score
    if len(latencies) == 0:
        # All workloads failed
        logger.error("All workloads failed")

        return EvaluationResult(
            metrics={
                "combined_score": -100000.0,  # Very bad score for all failures
                "avg_e2e_ms": float("inf"),
                "num_successful": 0,
                "num_failed": len(WORKLOADS),
                "error": "All workloads failed",
            },
            artifacts={
                "error_type": "AllWorkloadsFailed",
                "error_message": f"All {len(WORKLOADS)} workloads failed to run or parse",
                "failed_workloads": failed_workloads,
                "workload_results": workload_results,
                "suggestion": "Check BLIS simulation errors. May be routing logic causing crashes or timeouts.",
            },
        )

    # Per-workload mean improvement (each workload contributes equally)
    # ADRS finding: use smooth, proportional scoring — no discontinuous flat penalties
    mean_improvements = []
    regression_count = 0
    regression_penalty = 0.0
    REGRESSION_TOLERANCE_PCT = 1.0  # 1% tolerance before penalty kicks in
    REGRESSION_PENALTY_RATE = 3.0  # extra cost per percentage point of regression beyond tolerance

    for workload_name, workload_file in WORKLOADS:
        result = workload_results.get(workload_name)
        if result is None or result.get("e2e_ms") is None:
            continue
        actual_mean = result["e2e_ms"]
        baseline_mean = baseline_metrics.get(f"{workload_name}_e2e_ms")
        if baseline_mean and baseline_mean > 0:
            imp = (baseline_mean - actual_mean) / baseline_mean * 100.0
            mean_improvements.append(imp)
            if imp < -REGRESSION_TOLERANCE_PCT:
                regression_count += 1
                # Proportional penalty: 2% regression → 3pts, 5% → 12pts, 10% → 27pts
                excess = abs(imp) - REGRESSION_TOLERANCE_PCT
                regression_penalty += excess * REGRESSION_PENALTY_RATE

    score = (sum(mean_improvements) / len(mean_improvements)) if mean_improvements else -100.0
    score -= regression_penalty

    # Keep raw averages for backward compat / logging
    avg_latency = sum(latencies) / len(latencies)
    avg_tail_latency = sum(tail_latencies) / len(tail_latencies)

    # Calculate success rate
    success_rate = len(latencies) / len(WORKLOADS)

    # Log evaluation summary with improvement percentages
    summary_lines = ["EVALUATION COMPLETE"]
    for workload_name, workload_file in WORKLOADS:
        result = workload_results.get(workload_name)
        if result is not None and result.get("e2e_ms") is not None:
            baseline_mean = baseline_metrics.get(f"{workload_name}_e2e_ms")
            imp_str = ""
            if baseline_mean and baseline_mean > 0:
                imp = (baseline_mean - result["e2e_ms"]) / baseline_mean * 100.0
                imp_str = f" ({imp:+.1f}%)"
            summary_lines.append(
                f"  {workload_name}: mean={result['e2e_ms']:.0f}ms{imp_str}"
            )
        else:
            summary_lines.append(f"  {workload_name}: FAILED")
    avg_imp = (sum(mean_improvements) / len(mean_improvements)) if mean_improvements else 0.0
    summary_lines.append(
        f"  Avg improvement={avg_imp:+.1f}% regressions={regression_count} | Score: {score:.2f}"
    )
    logger.info(" | ".join(summary_lines))

    # Prepare artifacts
    artifacts = {
        "workload_results": workload_results,
        "successful_workloads": len(latencies),
        "failed_workloads": len(failed_workloads),
        "success_rate": f"{success_rate:.0%}",
    }

    if failed_workloads:
        artifacts["warning"] = f"Some workloads failed: {', '.join(failed_workloads)}"
        artifacts["suggestion"] = (
            "Check if evolved routing logic causes crashes or extreme slowdowns"
        )

    # Hypothesis testing
    actual_for_hypothesis = {
        "cache_warmup_e2e_ms": workload_results.get("cache_warmup", {}).get("e2e_ms"),
        "load_spikes_e2e_ms": workload_results.get("load_spikes", {}).get("e2e_ms"),
        "multiturn_e2e_ms": workload_results.get("multiturn", {}).get("e2e_ms"),
        "avg_e2e_ms": avg_latency if latencies else None,
        "avg_p95_ms": avg_tail_latency if tail_latencies else None,
    }
    actual_for_hypothesis = {k: v for k, v in actual_for_hypothesis.items() if v is not None}

    if hypotheses:
        h_results = test_hypotheses(hypotheses, actual_for_hypothesis, baseline_metrics)
        baseline_score = baseline_metrics.get("combined_score", 0)
        hypothesis_results_text = format_hypothesis_results(h_results, score, baseline_score)
        logger.info(f"Hypothesis results:\n{hypothesis_results_text}")

        ledger_path = script_dir / "openevolve_output" / "hypothesis_ledger.json"
        ledger = load_ledger(ledger_path)
        if not ledger["baseline"] and baseline_metrics:
            ledger["baseline"] = baseline_metrics
        update_ledger(ledger, h_results, score, ledger_path)
        knowledge_base_text = generate_knowledge_base_summary(ledger)

        artifacts["hypothesis_results"] = hypothesis_results_text
        artifacts["hypothesis_knowledge_base"] = knowledge_base_text
    else:
        # Still show knowledge base even without hypotheses in this iteration
        ledger_path = script_dir / "openevolve_output" / "hypothesis_ledger.json"
        if ledger_path.exists():
            ledger = load_ledger(ledger_path)
            knowledge_base_text = generate_knowledge_base_summary(ledger)
            if knowledge_base_text:
                artifacts["hypothesis_knowledge_base"] = knowledge_base_text

    # Return metrics
    metrics = {
        "combined_score": score,
        "avg_e2e_ms": avg_latency,
        "avg_p95_ms": avg_tail_latency,
        "avg_mean_improvement_pct": (sum(mean_improvements) / len(mean_improvements)) if mean_improvements else 0.0,
        "regression_count": regression_count,
        # V2 workload metrics
        "cache_warmup_e2e_ms": workload_results.get("cache_warmup", {}).get("e2e_ms"),
        "load_spikes_e2e_ms": workload_results.get("load_spikes", {}).get("e2e_ms"),
        "multiturn_e2e_ms": workload_results.get("multiturn", {}).get("e2e_ms"),
        "success_rate": success_rate,
        "num_successful": len(latencies),
        "num_failed": len(failed_workloads),
    }

    return EvaluationResult(metrics=metrics, artifacts=artifacts)


if __name__ == "__main__":
    # Test the evaluator with the initial program
    print("Testing evaluator with initial program...")

    script_dir = Path(__file__).parent
    initial_program_path = script_dir / "initial_program.py"

    # Evaluate it by passing the file path
    result = evaluate(str(initial_program_path))

    print("\nTest result:")
    score = result.metrics.get("combined_score")
    avg_e2e = result.metrics.get("avg_e2e_ms")
    success_rate = result.metrics.get("success_rate")

    print(f"  Score: {score:.2f}" if score is not None else "  Score: N/A")
    print(
        f"  Avg E2E: {avg_e2e:.2f}ms"
        if avg_e2e is not None and avg_e2e != float("inf")
        else "  Avg E2E: N/A"
    )
    print(
        f"  Success rate: {success_rate:.0%}" if success_rate is not None else "  Success rate: N/A"
    )
