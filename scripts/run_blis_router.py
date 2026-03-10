"""
Run BLIS Router optimization using SkyDiscover.

Outputs are organized by algorithm and model:
  blis_router_results/{algorithm}_{model_short}/{timestamp}/

Prerequisites:
  1. Go installed and on PATH
  2. inference-sim cloned and built (see benchmarks/blis_router/BLIS_ROUTER_SETUP.md)
  3. OPENAI_API_KEY environment variable set

Run from project root:
  uv run python scripts/run_blis_router.py
  uv run python scripts/run_blis_router.py --search openevolve_native --iterations 50
  uv run python scripts/run_blis_router.py --search shinkaevolve --model gcp/claude-sonnet-4-5
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Fix Windows console encoding for UTF-8 characters (→, —, 🌟, etc.) logged by
# OpenEvolve/SkyDiscover internals.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # Patch logging.StreamHandler.emit to sanitize output for the stream's
    # encoding *before* writing.  The reactive try/except approach doesn't work
    # because Python's logging module prints its own "--- Logging error ---"
    # traceback before re-raising the UnicodeEncodeError to our handler.
    import logging as _logging
    _orig_emit = _logging.StreamHandler.emit
    def _safe_emit(self, record):
        # Determine the target encoding so we can pre-sanitize
        enc = getattr(getattr(self, "stream", None), "encoding", None) or "utf-8"
        if isinstance(record.msg, str):
            record.msg = record.msg.encode(enc, errors="replace").decode(enc)
        if record.args:
            sanitized = []
            for a in record.args:
                if isinstance(a, str):
                    sanitized.append(a.encode(enc, errors="replace").decode(enc))
                else:
                    sanitized.append(a)
            record.args = tuple(sanitized)
        _orig_emit(self, record)
    _logging.StreamHandler.emit = _safe_emit

from skydiscover import run_discovery


# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_SEARCH = "adaevolve"
DEFAULT_MODEL = "gcp/gemini-3-flash-preview"
DEFAULT_API_BASE = "https://ete-litellm.ai-models.vpc-int.res.ibm.com"
DEFAULT_ITERATIONS = 2  # Start small; increase to 50-100 for real runs

RESULTS_ROOT = Path("blis_router_results")


# ── Helpers ───────────────────────────────────────────────────────────────────

def model_short_name(model: str) -> str:
    """gcp/gemini-3-flash-preview  ->  gemini-3-flash-preview"""
    return model.split("/", 1)[-1] if "/" in model else model


def build_output_dir(search: str, model: str) -> Path:
    """Build organized output path: blis_router_results/{algo}_{model}/{timestamp}/"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return RESULTS_ROOT / f"{search}_{model_short_name(model)}" / ts


def append_to_scores_log(output_dir: Path, search: str, model: str,
                         iterations: int, result) -> None:
    """Append a one-line JSON summary to blis_router_results/scores.jsonl"""
    scores_file = RESULTS_ROOT / "scores.jsonl"
    scores_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now().isoformat(),
        "search": search,
        "model": model,
        "iterations": iterations,
        "best_score": result.best_score,
        "initial_score": result.initial_score,
        "output_dir": str(output_dir),
        "metrics": result.metrics if hasattr(result, "metrics") else {},
    }
    with open(scores_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Run BLIS Router benchmark with SkyDiscover",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Available search algorithms:
  adaevolve          Multi-island adaptive search with UCB, migration, breakthroughs
  evox               Self-evolving paradigm with co-adaptive experience management
  topk               Selects top-K solutions to refine
  beam_search        Breadth-first expansion of a beam of top solutions
  best_of_n          Generates N variants per iteration, keeps the best
  gepa_native        Pareto-efficient search with reflective prompting + LLM merge
  openevolve_native  MAP-Elites + island-based evolutionary search (OpenEvolve port)
  shinkaevolve       Multi-patch evolution with UCB1 LLM selection (external)
""",
    )
    p.add_argument("--search", "-s", default=DEFAULT_SEARCH,
                    help=f"Search algorithm (default: {DEFAULT_SEARCH})")
    p.add_argument("--model", "-m", default=DEFAULT_MODEL,
                    help=f"LLM model (default: {DEFAULT_MODEL})")
    p.add_argument("--api-base", default=DEFAULT_API_BASE,
                    help="LiteLLM proxy URL")
    p.add_argument("--iterations", "-i", type=int, default=DEFAULT_ITERATIONS,
                    help=f"Number of iterations (default: {DEFAULT_ITERATIONS})")
    p.add_argument("--output-dir", "-o", default=None,
                    help="Override output directory (default: auto-generated)")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else build_output_dir(
        args.search, args.model
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Algorithm : {args.search}")
    print(f"Model     : {args.model}")
    print(f"Iterations: {args.iterations}")
    print(f"Output    : {output_dir}")
    print()

    result = run_discovery(
        initial_program="benchmarks/blis_router/initial_program.py",
        evaluator="benchmarks/blis_router/evaluator.py",
        config="benchmarks/blis_router/config.yaml",
        search=args.search,
        model=args.model,
        api_base=args.api_base,
        output_dir=str(output_dir),
        iterations=args.iterations,
    )

    print(f"\nBest score: {result.best_score}")
    print(f"Best solution:\n{result.best_solution}")

    # Log to centralized scores file
    append_to_scores_log(output_dir, args.search, args.model,
                         args.iterations, result)
    print(f"\nScore logged to {RESULTS_ROOT / 'scores.jsonl'}")


if __name__ == "__main__":
    main()
