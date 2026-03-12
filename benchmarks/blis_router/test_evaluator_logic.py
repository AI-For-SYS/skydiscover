"""
Regression / equivalence tests for evaluator.py.

Two goals:
  1. Unit-test pure functions (parsing, extraction, EVOLVE-BLOCK enforcement).
  2. Score equivalence: given the same program + fake workload outputs, old and
     new evaluate() must return the same result dict (all fields, not just score).

Run with:
    uv run python -m pytest benchmarks/blis_router/test_evaluator_logic.py -v
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BLIS_DIR = Path(__file__).parent
sys.path.insert(0, str(BLIS_DIR))

import evaluator
from evaluator import (
    _parse_cluster_metrics,
    enforce_evolve_block_boundary,
    extract_evolve_block,
    extract_go_code,
)

# ---------------------------------------------------------------------------
# Shared constants / test Go code
# ---------------------------------------------------------------------------

TEMPLATE_GO = """\
package sim

import (
\t"fmt"
)

func (ws *WeightedScoring) Route(req *Request, state *RouterState) RoutingDecision {
\tsnapshots := state.Snapshots

\t// EVOLVE-BLOCK-START
\t// original body
\tbestIdx := 0
\t// EVOLVE-BLOCK-END

\treturn NewRoutingDecision(snapshots[bestIdx].ID, fmt.Sprintf("ok"))
}
"""

EVOLVED_GO_BLOCK_ONLY = """\
package sim

import (
\t"fmt"
)

func (ws *WeightedScoring) Route(req *Request, state *RouterState) RoutingDecision {
\tsnapshots := state.Snapshots

\t// EVOLVE-BLOCK-START
\t// improved body
\tbestIdx := 1
\t// EVOLVE-BLOCK-END

\treturn NewRoutingDecision(snapshots[bestIdx].ID, fmt.Sprintf("ok"))
}
"""

EVOLVED_GO_OUTSIDE_BLOCK = """\
package sim

import (
\t"fmt"
)

// MODIFIED outside the block — should be reverted to template
func (ws *WeightedScoring) Route(req *Request, state *RouterState) RoutingDecision {
\tsnapshots := state.Snapshots
\textra_variable := 42

\t// EVOLVE-BLOCK-START
\t// new improved body
\tbestIdx := 2
\t// EVOLVE-BLOCK-END

\treturn NewRoutingDecision(snapshots[bestIdx].ID, fmt.Sprintf("ok"))
}
"""

EVOLVED_GO_NEW_IMPORT = """\
package sim

import (
\t"fmt"
\t"math"
)

func (ws *WeightedScoring) Route(req *Request, state *RouterState) RoutingDecision {
\tsnapshots := state.Snapshots

\t// EVOLVE-BLOCK-START
\t_ = math.Sqrt(1.0)
\tbestIdx := 0
\t// EVOLVE-BLOCK-END

\treturn NewRoutingDecision(snapshots[bestIdx].ID, fmt.Sprintf("ok"))
}
"""

PYTHON_WRAPPER = '''\
"""Initial Program"""

GO_ROUTING_CODE = """{}"""
'''

# Fake baseline: all three workloads at 4000ms mean / 8000ms p95
FAKE_BASELINE = {
    "cache_warmup_e2e_ms": 4000.0,
    "load_spikes_e2e_ms": 4000.0,
    "multiturn_e2e_ms": 4000.0,
    "avg_e2e_ms": 4000.0,
    "avg_p95_ms": 8000.0,
    "combined_score": 0.0,
}


def _cluster_json(e2e_mean: float, e2e_p95: float, n: int = 100) -> str:
    return json.dumps(
        {
            "instance_id": "cluster",
            "e2e_mean_ms": e2e_mean,
            "e2e_p95_ms": e2e_p95,
            "completed_requests": n,
        }
    )


def _side_effects(e2e_mean: float = 3600.0, e2e_p95: float = 7200.0, n: int = 100):
    """One successful build + three successful workload runs."""
    build_ok = MagicMock(returncode=0, stdout="", stderr="")
    workload_ok = MagicMock(returncode=0, stdout=_cluster_json(e2e_mean, e2e_p95, n), stderr="")
    return [build_ok, workload_ok, workload_ok, workload_ok]


# ---------------------------------------------------------------------------
# _parse_cluster_metrics
# ---------------------------------------------------------------------------


class TestParseClusterMetrics:
    def test_extracts_cluster_block(self):
        output = (
            '{"instance_id": "worker-0", "e2e_mean_ms": 100.0}\n'
            '{"instance_id": "cluster", "e2e_mean_ms": 4200.5, "e2e_p95_ms": 8100.3, "completed_requests": 150}\n'
        )
        r = _parse_cluster_metrics(output)
        assert r is not None
        assert r["instance_id"] == "cluster"
        assert r["e2e_mean_ms"] == pytest.approx(4200.5)
        assert r["e2e_p95_ms"] == pytest.approx(8100.3)
        assert r["completed_requests"] == 150

    def test_returns_none_when_no_cluster(self):
        assert _parse_cluster_metrics('{"instance_id": "worker-0", "e2e_mean_ms": 100.0}') is None

    def test_returns_none_on_empty_output(self):
        assert _parse_cluster_metrics("") is None
        assert _parse_cluster_metrics("no json here\n") is None

    def test_skips_malformed_json(self):
        assert _parse_cluster_metrics('{\n  "instance_id": "cluster",\n  INVALID\n}') is None

    def test_multiline_json_block(self):
        output = (
            '{\n  "instance_id": "cluster",\n'
            '  "e2e_mean_ms": 1.0,\n  "e2e_p95_ms": 2.0\n}\n'
        )
        r = _parse_cluster_metrics(output)
        assert r["e2e_mean_ms"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# extract_evolve_block
# ---------------------------------------------------------------------------


class TestExtractEvolveBlock:
    def test_extracts_block_content(self):
        block = extract_evolve_block(TEMPLATE_GO)
        assert "original body" in block
        assert "bestIdx := 0" in block
        assert "EVOLVE-BLOCK-START" not in block
        assert "EVOLVE-BLOCK-END" not in block

    def test_returns_empty_when_no_markers(self):
        assert extract_evolve_block("package sim\nfunc foo() {}\n") == ""

    def test_returns_empty_with_only_start_marker(self):
        assert extract_evolve_block("// EVOLVE-BLOCK-START\nbody\n") == ""


# ---------------------------------------------------------------------------
# enforce_evolve_block_boundary
# ---------------------------------------------------------------------------


class TestEnforceEvolveBlockBoundary:
    def test_splices_evolved_block(self):
        r = enforce_evolve_block_boundary(EVOLVED_GO_BLOCK_ONLY, TEMPLATE_GO)
        assert "improved body" in r
        assert "bestIdx := 1" in r
        assert "return NewRoutingDecision" in r

    def test_reverts_out_of_block_changes(self):
        r = enforce_evolve_block_boundary(EVOLVED_GO_OUTSIDE_BLOCK, TEMPLATE_GO)
        assert "extra_variable" not in r
        assert "MODIFIED outside" not in r
        assert "new improved body" in r
        assert "bestIdx := 2" in r

    def test_carries_over_new_imports(self):
        r = enforce_evolve_block_boundary(EVOLVED_GO_NEW_IMPORT, TEMPLATE_GO)
        assert '"math"' in r
        assert "math.Sqrt" in r

    def test_passes_through_when_no_markers_in_evolved(self):
        code = "package sim\nfunc foo() {}\n"
        assert enforce_evolve_block_boundary(code, TEMPLATE_GO) == code

    def test_passes_through_when_no_markers_in_template(self):
        template = "package sim\nfunc foo() {}\n"
        assert enforce_evolve_block_boundary(EVOLVED_GO_BLOCK_ONLY, template) == EVOLVED_GO_BLOCK_ONLY

    def test_idempotent_on_initial_program(self):
        r = enforce_evolve_block_boundary(TEMPLATE_GO, TEMPLATE_GO)
        assert extract_evolve_block(r) == extract_evolve_block(TEMPLATE_GO)


# ---------------------------------------------------------------------------
# extract_go_code
# ---------------------------------------------------------------------------


class TestExtractGoCode:
    def test_extracts_from_python_wrapper(self):
        go = "package sim\nfunc foo() {}\n"
        # extract_go_code strips surrounding whitespace from the triple-quoted string
        assert extract_go_code(PYTHON_WRAPPER.format(go)) == go.strip()

    def test_extracts_bare_go_code(self):
        go = "package sim\nfunc foo() {}\n"
        assert extract_go_code(go) == go

    def test_returns_empty_on_unrecognised_input(self):
        assert extract_go_code("just some python\nx = 1\n") == ""

    def test_extracts_from_actual_initial_program(self):
        p = BLIS_DIR / "initial_program.py"
        if not p.exists():
            pytest.skip("initial_program.py not present")
        r = extract_go_code(p.read_text(encoding="utf-8"))
        assert r.startswith("package sim")
        assert "EVOLVE-BLOCK-START" in r
        assert "EVOLVE-BLOCK-END" in r


# ---------------------------------------------------------------------------
# Helpers for mocked evaluate() calls
# ---------------------------------------------------------------------------


def _call_new_evaluate(program_path: str, slot_dir: Path) -> dict:
    """
    Run new evaluate() with subprocess fully mocked (build + 3 workloads)
    and _acquire_slot pointing to a temp slot dir.
    """
    fake_lock = MagicMock()
    (slot_dir / "sim").mkdir(parents=True, exist_ok=True)
    with (
        patch("evaluator.get_or_compute_baseline", return_value=FAKE_BASELINE),
        patch("evaluator._acquire_slot", return_value=(0, slot_dir, fake_lock)),
        patch("evaluator.subprocess") as mock_sub,
    ):
        mock_sub.run.side_effect = _side_effects()
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        return evaluator.evaluate(program_path)


def _call_old_evaluate(old_mod, program_path: str, slot_dir: Path) -> dict:
    """
    Run old evaluate() with the same mocks.
    The routing.go write (which targets inference-sim/sim/ in the old code) is
    redirected to slot_dir/sim/routing.go via a selective builtins.open patch
    so no real inference-sim checkout is needed.
    """
    real_open = open
    routing_go_target = slot_dir / "sim" / "routing.go"
    routing_go_target.parent.mkdir(parents=True, exist_ok=True)

    def patched_open(path, mode="r", **kwargs):
        # Redirect routing.go writes only; pass everything else through.
        if "w" in str(mode) and Path(path).name == "routing.go":
            return real_open(routing_go_target, mode, **kwargs)
        return real_open(path, mode, **kwargs)

    with (
        patch.object(old_mod, "get_or_compute_baseline", return_value=FAKE_BASELINE),
        patch.object(old_mod, "subprocess") as mock_sub,
        patch("builtins.open", side_effect=patched_open),
    ):
        mock_sub.run.side_effect = _side_effects()
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        return old_mod.evaluate(program_path)


# ---------------------------------------------------------------------------
# Score-equivalence: old vs new evaluate() with mocked subprocess
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def old_evaluator_module():
    """
    Load pre-slot-pool evaluator (commit 4d43d85) as a Python module.
    Saved to BLIS_DIR so that Path(__file__).parent inside it resolves to
    the same script_dir as the new evaluator.
    """
    try:
        result = subprocess.run(
            ["git", "show", "4d43d85:benchmarks/blis_router/evaluator.py"],
            cwd=BLIS_DIR.parent.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if result.returncode != 0:
            pytest.skip("Could not fetch old evaluator from git")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("git not available")

    old_path = BLIS_DIR / "_evaluator_v1_test_only.py"
    old_path.write_text(result.stdout, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("evaluator_v1", str(old_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod

    old_path.unlink(missing_ok=True)


@pytest.fixture
def program_file(tmp_path):
    p = tmp_path / "program.py"
    p.write_text(PYTHON_WRAPPER.format(TEMPLATE_GO), encoding="utf-8")
    return str(p)


@pytest.fixture
def slot_dir(tmp_path):
    return tmp_path / "slot_0"


class TestScoreEquivalence:
    """
    Core equivalence: same program input must yield an identical result dict
    from old (single-file) and new (slot-pool) evaluate().
    """

    FLOAT_KEYS = (
        "combined_score",
        "avg_e2e_ms",
        "avg_p95_ms",
        "avg_mean_improvement_pct",
        "cache_warmup_e2e_ms",
        "load_spikes_e2e_ms",
        "multiturn_e2e_ms",
        "success_rate",
    )
    INT_KEYS = ("regression_count", "num_successful", "num_failed")

    def test_all_metrics_identical(self, old_evaluator_module, program_file, slot_dir):
        old = _call_old_evaluate(old_evaluator_module, program_file, slot_dir)
        new = _call_new_evaluate(program_file, slot_dir)

        for key in self.FLOAT_KEYS:
            assert key in old, f"old result missing key: {key}"
            assert key in new, f"new result missing key: {key}"
            assert old[key] == pytest.approx(new[key], abs=1e-6), (
                f"Mismatch on {key!r}: old={old[key]}, new={new[key]}"
            )
        for key in self.INT_KEYS:
            assert key in old, f"old result missing key: {key}"
            assert key in new, f"new result missing key: {key}"
            assert old[key] == new[key], f"Mismatch on {key!r}: old={old[key]}, new={new[key]}"

    def test_artifacts_workload_results_identical(
        self, old_evaluator_module, program_file, slot_dir
    ):
        old = _call_old_evaluate(old_evaluator_module, program_file, slot_dir)
        new = _call_new_evaluate(program_file, slot_dir)

        old_wr = old["artifacts"]["workload_results"]
        new_wr = new["artifacts"]["workload_results"]
        assert set(old_wr) == set(new_wr), "Workload names differ"
        for wl in old_wr:
            assert old_wr[wl]["e2e_ms"] == pytest.approx(new_wr[wl]["e2e_ms"], abs=1e-6)
            assert old_wr[wl]["e2e_p95_ms"] == pytest.approx(new_wr[wl]["e2e_p95_ms"], abs=1e-6)
            assert old_wr[wl]["completed_requests"] == new_wr[wl]["completed_requests"]

    def test_artifacts_counts_identical(self, old_evaluator_module, program_file, slot_dir):
        old = _call_old_evaluate(old_evaluator_module, program_file, slot_dir)
        new = _call_new_evaluate(program_file, slot_dir)

        assert old["artifacts"]["successful_workloads"] == new["artifacts"]["successful_workloads"]
        assert old["artifacts"]["failed_workloads"] == new["artifacts"]["failed_workloads"]
        assert old["artifacts"]["success_rate"] == new["artifacts"]["success_rate"]


# ---------------------------------------------------------------------------
# New evaluate() correctness: absolute expected values
# ---------------------------------------------------------------------------


class TestEvaluateCorrectness:
    """
    Verify absolute correctness of the result dict for known inputs,
    independent of the old version.  Tests the scoring formula end-to-end.
    """

    @pytest.mark.parametrize(
        "e2e_mean, e2e_p95, exp_score, exp_improvement, exp_regressions",
        [
            # 10% improvement on all 3 workloads, no regressions
            (3600.0, 7200.0, 10.0, 10.0, 0),
            # Baseline — no change
            (4000.0, 8000.0, 0.0, 0.0, 0),
            # 10% regression on all 3 workloads
            # imp = -10%, tolerance=1%, excess=9%, penalty_per_wl = 9*3=27, total=81
            # score = -10 - 81 = -91
            (4400.0, 8800.0, -91.0, -10.0, 3),
        ],
    )
    def test_score_and_metrics(
        self,
        tmp_path,
        e2e_mean,
        e2e_p95,
        exp_score,
        exp_improvement,
        exp_regressions,
    ):
        slot_dir = tmp_path / "slot_0"
        (slot_dir / "sim").mkdir(parents=True)
        fake_lock = MagicMock()

        program_file = tmp_path / "program.py"
        program_file.write_text(PYTHON_WRAPPER.format(TEMPLATE_GO), encoding="utf-8")

        with (
            patch("evaluator.get_or_compute_baseline", return_value=FAKE_BASELINE),
            patch("evaluator._acquire_slot", return_value=(0, slot_dir, fake_lock)),
            patch("evaluator.subprocess") as mock_sub,
        ):
            mock_sub.run.side_effect = _side_effects(e2e_mean, e2e_p95)
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            result = evaluator.evaluate(str(program_file))

        assert result["combined_score"] == pytest.approx(exp_score, abs=0.01)
        assert result["avg_mean_improvement_pct"] == pytest.approx(exp_improvement, abs=0.01)
        assert result["regression_count"] == exp_regressions
        assert result["avg_e2e_ms"] == pytest.approx(e2e_mean, abs=1e-6)
        assert result["avg_p95_ms"] == pytest.approx(e2e_p95, abs=1e-6)
        assert result["num_successful"] == 3
        assert result["num_failed"] == 0
        assert result["success_rate"] == pytest.approx(1.0)
        # Per-workload e2e values
        assert result["cache_warmup_e2e_ms"] == pytest.approx(e2e_mean)
        assert result["load_spikes_e2e_ms"] == pytest.approx(e2e_mean)
        assert result["multiturn_e2e_ms"] == pytest.approx(e2e_mean)
        # Artifacts
        assert result["artifacts"]["successful_workloads"] == 3
        assert result["artifacts"]["failed_workloads"] == 0

    def test_build_failure_returns_minus_100000(self, tmp_path):
        slot_dir = tmp_path / "slot_0"
        (slot_dir / "sim").mkdir(parents=True)
        fake_lock = MagicMock()

        program_file = tmp_path / "program.py"
        program_file.write_text(PYTHON_WRAPPER.format(TEMPLATE_GO), encoding="utf-8")

        with (
            patch("evaluator.get_or_compute_baseline", return_value=FAKE_BASELINE),
            patch("evaluator._acquire_slot", return_value=(0, slot_dir, fake_lock)),
            patch("evaluator.subprocess") as mock_sub,
        ):
            mock_sub.run.return_value = MagicMock(returncode=1, stdout="", stderr="syntax error")
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            result = evaluator.evaluate(str(program_file))

        assert result["combined_score"] == -100000.0
        assert result["artifacts"]["error_type"] == "BuildError"

    def test_all_workloads_fail_returns_minus_100000(self, tmp_path):
        slot_dir = tmp_path / "slot_0"
        (slot_dir / "sim").mkdir(parents=True)
        fake_lock = MagicMock()

        program_file = tmp_path / "program.py"
        program_file.write_text(PYTHON_WRAPPER.format(TEMPLATE_GO), encoding="utf-8")

        build_ok = MagicMock(returncode=0, stdout="", stderr="")
        workload_fail = MagicMock(returncode=1, stdout="", stderr="sim error")

        with (
            patch("evaluator.get_or_compute_baseline", return_value=FAKE_BASELINE),
            patch("evaluator._acquire_slot", return_value=(0, slot_dir, fake_lock)),
            patch("evaluator.subprocess") as mock_sub,
        ):
            mock_sub.run.side_effect = [build_ok, workload_fail, workload_fail, workload_fail]
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            result = evaluator.evaluate(str(program_file))

        assert result["combined_score"] == -100000.0
        assert result["num_successful"] == 0
        assert result["num_failed"] == 3
        assert result["artifacts"]["error_type"] == "AllWorkloadsFailed"
        fake_lock.release.assert_called_once()

    def test_slot_lock_always_released(self, tmp_path):
        """Lock must be released even when a workload raises an OS exception."""
        slot_dir = tmp_path / "slot_0"
        (slot_dir / "sim").mkdir(parents=True)
        fake_lock = MagicMock()

        program_file = tmp_path / "program.py"
        program_file.write_text(PYTHON_WRAPPER.format(TEMPLATE_GO), encoding="utf-8")

        build_ok = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("evaluator.get_or_compute_baseline", return_value=FAKE_BASELINE),
            patch("evaluator._acquire_slot", return_value=(0, slot_dir, fake_lock)),
            patch("evaluator.subprocess") as mock_sub,
        ):
            mock_sub.run.side_effect = [build_ok, OSError("disk full"), OSError(), OSError()]
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            evaluator.evaluate(str(program_file))

        fake_lock.release.assert_called_once()

    def test_invalid_go_code_returns_minus_100000(self, tmp_path):
        slot_dir = tmp_path / "slot_0"
        (slot_dir / "sim").mkdir(parents=True)
        fake_lock = MagicMock()

        program_file = tmp_path / "program.py"
        program_file.write_text("x = 1  # no Go code here\n", encoding="utf-8")

        with (
            patch("evaluator.get_or_compute_baseline", return_value=FAKE_BASELINE),
            patch("evaluator._acquire_slot", return_value=(0, slot_dir, fake_lock)),
            patch("evaluator.subprocess") as mock_sub,
        ):
            mock_sub.TimeoutExpired = subprocess.TimeoutExpired
            result = evaluator.evaluate(str(program_file))

        assert result["combined_score"] == -100000.0
        assert result["artifacts"]["error_type"] == "ExtractionError"
        # Extraction error is inside the try: block, so finally: fires and releases the lock
        fake_lock.release.assert_called_once()


# ---------------------------------------------------------------------------
# Cross-version source check: scoring formula must be identical
# ---------------------------------------------------------------------------


class TestScoringFormulaSource:
    def test_scoring_section_identical(self, old_evaluator_module):
        import inspect

        old_src = inspect.getsource(old_evaluator_module.evaluate)
        new_src = inspect.getsource(evaluator.evaluate)

        def extract_scoring(src: str) -> str:
            lines = src.splitlines()
            in_section = False
            section = []
            for line in lines:
                if "Per-workload mean improvement" in line:
                    in_section = True
                if "Keep raw averages" in line and in_section:
                    break
                if in_section:
                    section.append(line.strip())
            return "\n".join(section)

        old_formula = extract_scoring(old_src)
        new_formula = extract_scoring(new_src)
        assert old_formula, "Could not locate scoring section in old evaluate()"
        assert new_formula, "Could not locate scoring section in new evaluate()"
        assert old_formula == new_formula, (
            "Scoring formula has drifted between old and new evaluate()!\n"
            f"Old:\n{old_formula}\n\nNew:\n{new_formula}"
        )
