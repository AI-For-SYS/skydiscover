"""
Hypothesis parsing, testing, and ledger management for BLIS router evolution.

The LLM is prompted to embed structured hypothesis comments in the Go code:
    // HYPOTHESIS-1: <claim>
    // MECHANISM-1: <mechanism description>
    //   <continuation lines with extra indentation>
    // EXPECT-1: <metric> < <threshold>

This module parses those comments, tests them against actual evaluation
results, and maintains a persistent ledger of hypothesis outcomes across
iterations. The ledger feeds back into the LLM prompt as a knowledge base.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Metrics that hypothesis EXPECT lines may reference.
# Kept in sync with evaluator.py output keys.
VALID_METRICS = {
    "cache_warmup_e2e_ms",
    "load_spikes_e2e_ms",
    "multiturn_e2e_ms",
    "avg_e2e_ms",
    "avg_p95_ms",
}


def parse_hypotheses(go_code: str) -> list:
    """Parse HYPOTHESIS-N, MECHANISM-N, EXPECT-N comment blocks from Go code.

    Returns a list of dicts, one per complete hypothesis (all three fields
    present).  Incomplete hypotheses (missing any field) are silently skipped.

    Each dict has keys:
        id        - int, the hypothesis number N
        claim     - str, the HYPOTHESIS-N text
        mechanism - str, the MECHANISM-N text (multi-line joined with spaces)
        metric    - str, the metric name from EXPECT-N
        threshold - float, the numeric threshold from EXPECT-N

    Multi-line MECHANISM is detected by continuation lines starting with
    ``//   `` (two slashes followed by three or more spaces).
    """
    # Collect raw fields keyed by hypothesis id
    # {id: {"claim": str, "mechanism": str, "metric": str, "threshold": float}}
    fields: dict[int, dict] = {}

    lines = go_code.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # HYPOTHESIS-N: <claim>
        m = re.match(r"^//\s*HYPOTHESIS-(\d+):\s*(.+)$", line)
        if m:
            hid = int(m.group(1))
            fields.setdefault(hid, {})["claim"] = m.group(2).strip()
            i += 1
            continue

        # MECHANISM-N: <mechanism> (possibly multi-line)
        m = re.match(r"^//\s*MECHANISM-(\d+):\s*(.+)$", line)
        if m:
            hid = int(m.group(1))
            parts = [m.group(2).strip()]
            # Consume continuation lines: "//   " (3+ spaces after //)
            while i + 1 < len(lines):
                next_line = lines[i + 1]
                # Continuation: starts with // followed by 3+ spaces (not a new tag)
                cont = re.match(r"^//\s{3,}(.+)$", next_line.strip())
                if cont:
                    parts.append(cont.group(1).strip())
                    i += 1
                else:
                    break
            fields.setdefault(hid, {})["mechanism"] = " ".join(parts)
            i += 1
            continue

        # EXPECT-N: <metric> < <threshold>
        m = re.match(r"^//\s*EXPECT-(\d+):\s*(\S+)\s*<\s*([0-9]+(?:\.[0-9]+)?)\s*$", line)
        if m:
            hid = int(m.group(1))
            entry = fields.setdefault(hid, {})
            entry["metric"] = m.group(2).strip()
            entry["threshold"] = float(m.group(3))
            i += 1
            continue

        i += 1

    # Build result list — only complete hypotheses with valid metrics
    results = []
    for hid in sorted(fields):
        f = fields[hid]
        if all(k in f for k in ("claim", "mechanism", "metric", "threshold")):
            if f["metric"] not in VALID_METRICS:
                logger.warning(
                    "Hypothesis %d references unknown metric %r; skipping",
                    hid,
                    f["metric"],
                )
                continue
            results.append(
                {
                    "id": hid,
                    "claim": f["claim"],
                    "mechanism": f["mechanism"],
                    "metric": f["metric"],
                    "threshold": f["threshold"],
                }
            )
    return results


def test_hypotheses(
    hypotheses: list,
    actual_metrics: dict,
    baseline_metrics: dict,
) -> list:
    """Test each hypothesis against actual evaluation results and baseline.

    Args:
        hypotheses: Output of parse_hypotheses().
        actual_metrics: Metric dict from this iteration's evaluation.
        baseline_metrics: Metric dict from the baseline evaluation.

    Returns:
        List of result dicts with keys:
            id, claim, mechanism, metric, threshold,
            actual, baseline_value, delta_vs_baseline_pct, verdict

        Verdicts:
            CONFIRMED   - actual < threshold
            REFUTED     - actual >= threshold
            INCONCLUSIVE - metric missing or None in actual_metrics
    """
    results = []
    for h in hypotheses:
        metric = h["metric"]
        threshold = h["threshold"]
        actual = actual_metrics.get(metric)
        baseline_value = baseline_metrics.get(metric)

        if actual is None:
            verdict = "INCONCLUSIVE"
            delta_pct = None
        else:
            verdict = "CONFIRMED" if actual < threshold else "REFUTED"
            if baseline_value is not None and baseline_value != 0:
                delta_pct = ((actual - baseline_value) / baseline_value) * 100.0
            else:
                delta_pct = None

        results.append(
            {
                "id": h["id"],
                "claim": h["claim"],
                "mechanism": h["mechanism"],
                "metric": metric,
                "threshold": threshold,
                "actual": actual,
                "baseline_value": baseline_value,
                "delta_vs_baseline_pct": delta_pct,
                "verdict": verdict,
            }
        )
    return results


def load_ledger(ledger_path: Path) -> dict:
    """Load hypothesis ledger from disk, or return empty structure.

    Returns:
        {"baseline": {}, "entries": []}
    """
    ledger_path = Path(ledger_path)
    if ledger_path.exists():
        with open(ledger_path, "r") as f:
            return json.load(f)
    return {"baseline": {}, "entries": []}


def update_ledger(
    ledger: dict,
    hypothesis_results: list,
    overall_combined_score: float,
    ledger_path: Path,
) -> dict:
    """Append hypothesis results to ledger and persist to disk.

    Each entry records the hypothesis results, overall combined score,
    and a UTC timestamp.

    Args:
        ledger: Current ledger dict (mutated in place and returned).
        hypothesis_results: Output of test_hypotheses().
        overall_combined_score: The combined_score from this evaluation.
        ledger_path: Path to write the updated ledger JSON.

    Returns:
        The updated ledger dict.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall_combined_score": overall_combined_score,
        "hypotheses": hypothesis_results,
    }
    ledger["entries"].append(entry)

    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "w") as f:
        json.dump(ledger, f, indent=2)
    return ledger


def generate_knowledge_base_summary(ledger: dict, top_n: int = 5) -> str:
    """Generate a text summary of hypothesis outcomes grouped by target metric.

    Sections:
        CONFIRMED STRATEGIES  - confirm_rate >= 0.5
        REFUTED STRATEGIES    - confirm_rate < 0.5, total >= 2
        INCONCLUSIVE          - total < 2
        BASELINE VALUES       - from ledger["baseline"]

    Confirmed sorted by avg_delta (most negative first = biggest improvement).
    Refuted sorted by avg_delta (most positive first = biggest regression).
    Each section limited to top_n entries.

    Returns "No hypothesis data yet." for empty ledger.
    """
    entries = ledger.get("entries", [])
    if not entries:
        lines = ["HYPOTHESIS KNOWLEDGE BASE:", "", "No hypothesis data yet."]
        baseline = ledger.get("baseline", {})
        if baseline:
            lines.append("")
            lines.append("BASELINE VALUES (initial program, static weights):")
            for k, v in sorted(baseline.items()):
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    # Aggregate per (metric, claim) pair
    # key = (metric, claim) -> list of {verdict, delta_pct}
    agg: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        for h in entry.get("hypotheses", []):
            key = (h["metric"], h["claim"])
            agg.setdefault(key, []).append(
                {
                    "verdict": h["verdict"],
                    "delta_pct": h.get("delta_vs_baseline_pct"),
                    "mechanism": h.get("mechanism", ""),
                }
            )

    # Compute stats per hypothesis
    stats = []
    for (metric, claim), records in agg.items():
        total = len(records)
        confirmed = sum(1 for r in records if r["verdict"] == "CONFIRMED")
        confirm_rate = confirmed / total if total > 0 else 0.0
        deltas = [r["delta_pct"] for r in records if r["delta_pct"] is not None]
        avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
        mechanism = records[0]["mechanism"]
        stats.append(
            {
                "metric": metric,
                "claim": claim,
                "mechanism": mechanism,
                "total": total,
                "confirmed": confirmed,
                "confirm_rate": confirm_rate,
                "avg_delta": avg_delta,
            }
        )

    # Partition into sections
    confirmed_strategies = [s for s in stats if s["confirm_rate"] >= 0.5]
    refuted_strategies = [s for s in stats if s["confirm_rate"] < 0.5 and s["total"] >= 2]
    inconclusive = [s for s in stats if s["total"] < 2 and s["confirm_rate"] < 0.5]

    # Sort
    confirmed_strategies.sort(key=lambda s: s["avg_delta"])  # most negative first
    refuted_strategies.sort(key=lambda s: -s["avg_delta"])  # most positive first

    # Limit
    confirmed_strategies = confirmed_strategies[:top_n]
    refuted_strategies = refuted_strategies[:top_n]
    inconclusive = inconclusive[:top_n]

    lines = []

    if confirmed_strategies:
        lines.append("=== CONFIRMED STRATEGIES ===")
        for s in confirmed_strategies:
            lines.append(
                f"  [{s['metric']}] {s['claim']} "
                f"(confirmed {s['confirmed']}/{s['total']}, avg_delta={s['avg_delta']:+.1f}%)"
            )
            if s["mechanism"]:
                lines.append(f"    mechanism: {s['mechanism']}")
        lines.append("")

    if refuted_strategies:
        lines.append("=== REFUTED STRATEGIES ===")
        for s in refuted_strategies:
            lines.append(
                f"  [{s['metric']}] {s['claim']} "
                f"(confirmed {s['confirmed']}/{s['total']}, avg_delta={s['avg_delta']:+.1f}%)"
            )
            if s["mechanism"]:
                lines.append(f"    mechanism: {s['mechanism']}")
        lines.append("")

    if inconclusive:
        lines.append("=== INCONCLUSIVE ===")
        for s in inconclusive:
            lines.append(f"  [{s['metric']}] {s['claim']} (total={s['total']})")
        lines.append("")

    # Baseline values
    baseline = ledger.get("baseline", {})
    if baseline:
        lines.append("=== BASELINE VALUES ===")
        for k, v in sorted(baseline.items()):
            lines.append(f"  {k}: {v}")
        lines.append("")

    return "\n".join(lines).rstrip()


def format_hypothesis_results(
    hypothesis_results: list,
    overall_score: float,
    baseline_score: float,
) -> str:
    """Format this iteration's hypothesis verdicts as human-readable text.

    Shows EXPECT vs ACTUAL for each hypothesis, plus OVERALL combined_score
    delta versus baseline.
    """
    lines = ["--- Hypothesis Verdicts ---"]
    for h in hypothesis_results:
        actual_str = f"{h['actual']:.1f}" if h["actual"] is not None else "N/A"
        delta_str = ""
        if h["delta_vs_baseline_pct"] is not None:
            delta_str = f" (delta={h['delta_vs_baseline_pct']:+.1f}% vs baseline)"
        lines.append(
            f"  H{h['id']} [{h['verdict']}]: "
            f"EXPECT {h['metric']} < {h['threshold']:.1f}, "
            f"ACTUAL {actual_str}{delta_str}"
        )
        lines.append(f"    claim: {h['claim']}")

    # Overall score delta
    score_delta = overall_score - baseline_score
    score_delta_pct = (score_delta / abs(baseline_score) * 100.0) if baseline_score != 0 else 0.0
    lines.append(
        f"  OVERALL: combined_score={overall_score:.2f} "
        f"(delta={score_delta:+.2f}, {score_delta_pct:+.1f}% vs baseline={baseline_score:.2f})"
    )
    return "\n".join(lines)
