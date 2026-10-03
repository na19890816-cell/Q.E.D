"""P8 メタ検証 (閾値感度 / 軸 ablation / 重み摂動) の単体テスト (DB レス)。"""
from __future__ import annotations

import dataclasses
import itertools
import json
from unittest.mock import MagicMock

import pytest

from analytics.python.frost import frost_meta_sensitivity as M
from analytics.python.frost.frost_config import FrostConfig
from analytics.python.pg_io import postgres_meta_validation_bridge as B
from tests.fixtures.frost_synthetic import make_synthetic_candidates

pytestmark = pytest.mark.p8_meta


@pytest.fixture(scope="module")
def cands():
    return make_synthetic_candidates(n=60, seed=7)


@pytest.fixture(scope="module")
def cfg():
    return FrostConfig(top_k=8, promotion_top_k=3)


@pytest.fixture(scope="module")
def prepared(cands, cfg):
    bundles = M.compute_bundles(cands, cfg)
    return bundles, M._decide(cands, bundles, cfg)


# ── 統計ユーティリティ ──────────────────────────────────────────────────

class TestJaccard:
    def test_identical(self):
        assert M.jaccard(["a", "b"], ["b", "a"]) == 1.0

    def test_disjoint(self):
        assert M.jaccard(["a"], ["b"]) == 0.0

    def test_partial(self):
        assert M.jaccard(["a", "b", "c"], ["b", "c", "d"]) == pytest.approx(0.5)

    def test_both_empty_is_stable(self):
        assert M.jaccard([], []) == 1.0


def _tau_brute(x, y):
    """τ-b の素朴な参照実装。"""
    c = d = tx = ty = 0
    for i, j in itertools.combinations(range(len(x)), 2):
        sx = (x[i] > x[j]) - (x[i] < x[j])
        sy = (y[i] > y[j]) - (y[i] < y[j])
        if sx == 0 and sy == 0:
            continue
        if sx == 0:
            tx += 1
        elif sy == 0:
            ty += 1
        elif sx == sy:
            c += 1
        else:
            d += 1
    return (c - d) / ((c + d + tx) * (c + d + ty)) ** 0.5


class TestKendallTau:
    def test_perfect(self):
        assert M.kendall_tau_b([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_reverse(self):
        assert M.kendall_tau_b([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)

    def test_known_value(self):
        # 1 discordant pair / 6 → (5-1)/6
        assert M.kendall_tau_b([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(4 / 6)

    def test_with_ties_matches_reference(self):
        x = [1, 2, 2, 3, 5, 5, 7]
        y = [2, 1, 3, 3, 6, 4, 7]
        assert M.kendall_tau_b(x, y) == pytest.approx(_tau_brute(x, y))

    def test_constant_is_none(self):
        assert M.kendall_tau_b([1, 1, 1], [1, 2, 3]) is None

    def test_short_is_none(self):
        assert M.kendall_tau_b([1], [1]) is None

    def test_length_mismatch(self):
        with pytest.raises(ValueError):
            M.kendall_tau_b([1, 2], [1])

    def test_no_scipy_or_statistics_import(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(M))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        assert not mods & {"statistics", "scipy", "numpy", "pandas"}


class TestDistribution:
    def test_summary(self):
        s = M.summarize_distribution([0.0, 1.0, 2.0, 3.0, 4.0])
        assert s["min"] == 0.0 and s["max"] == 4.0 and s["p50"] == 2.0 and s["mean"] == 2.0
        assert s["p05"] == pytest.approx(0.2)

    def test_empty(self):
        assert M.summarize_distribution([])["p50"] is None


# ── リプレイ ────────────────────────────────────────────────────────────

class TestReplay:
    def test_identity_compare(self, prepared):
        _, base = prepared
        m = M.compare_snapshots(base, base, gate="pbo", promotion_top_k=3)
        assert m["decision_flip_rate"] == 0.0
        assert m["topk_jaccard"] == 1.0 and m["promo_jaccard"] == 1.0
        assert m["kendall_tau"] == pytest.approx(1.0)
        assert m["gate_flip_rate"] == 0.0

    def test_rescore_identity(self, cands, cfg, prepared):
        bundles, base = prepared
        same = M.weight_replay(cands, bundles, cfg)
        assert same.scores == base.scores and same.decisions == base.decisions

    @pytest.mark.parametrize("w", M.V1_WEIGHT_FIELDS)
    def test_rescore_equals_full_replay(self, cands, cfg, prepared, w):
        """証拠キャッシュ再スコアはフルリプレイと厳密一致する (最適化の安全性)。"""
        bundles, _ = prepared
        c2 = dataclasses.replace(cfg, **{w: getattr(cfg, w) * 1.7 + 0.01})
        a = M.weight_replay(cands, bundles, c2)
        f = M.full_replay(cands, c2)
        assert a.scores == f.scores and a.decisions == f.decisions

    def test_rescore_does_not_mutate_bundles(self, cands, cfg, prepared):
        bundles, _ = prepared
        before = [b.scores.frost_score_v1 for b in bundles]
        M.rescore_bundles(bundles, dataclasses.replace(cfg, w_predictive=0.0))
        assert [b.scores.frost_score_v1 for b in bundles] == before

    def test_inputs_not_mutated(self, cfg):
        cs = make_synthetic_candidates(n=20, seed=3)
        snap = json.dumps([dataclasses.asdict(c) for c in cs], default=str, sort_keys=True)
        M.run_meta_validation(cs, cfg, n_runs=2, deltas=(0.1,))
        assert json.dumps([dataclasses.asdict(c) for c in cs], default=str, sort_keys=True) == snap

    def test_weight_fields_match_score_engine(self, cfg):
        from analytics.python.frost.score_engine import ScoreEngine
        assert set(M.V1_WEIGHT_FIELDS) == set(ScoreEngine.from_config(cfg)._get_v1_weights())

    def test_gate_fields_match_gate_engine(self):
        from analytics.python.frost.gate_engine import V1_GATE_NAMES
        assert list(M.GATE_THRESHOLD_FIELDS) == V1_GATE_NAMES


# ── 3 分析 ──────────────────────────────────────────────────────────────

class TestThresholdSensitivity:
    def test_perturb_clips(self, cfg):
        c = M.perturb_threshold(dataclasses.replace(cfg, pbo_threshold=0.9), "pbo_threshold", 0.2, True)
        assert c.pbo_threshold == 1.0

    def test_perturb_no_clip(self, cfg):
        c = M.perturb_threshold(cfg, "max_turnover", 0.2, False)
        assert c.max_turnover == pytest.approx(cfg.max_turnover * 1.2)

    def test_rows_and_ranges(self, cands, cfg, prepared):
        _, base = prepared
        rows = M.threshold_sensitivity(cands, cfg, base, deltas=(-0.2, 0.2))
        assert len(rows) == 8 * 2
        for r in rows:
            for k in ("decision_flip_rate", "gate_flip_rate", "topk_jaccard", "promo_jaccard"):
                assert 0.0 <= r.metrics[k] <= 1.0
            assert r.analysis_type == M.ANALYSIS_THRESHOLD

    def test_tighter_gate_lowers_pass_rate(self, cands, cfg, prepared):
        _, base = prepared
        rows = M.threshold_sensitivity(cands, cfg, base, deltas=(0.5,), gates=["max_drawdown"])
        # max_* を緩める (+50%) と通過率は下がらない
        assert rows[0].metrics["gate_pass_rate_perturbed"] >= rows[0].metrics["gate_pass_rate_base"]
        rows = M.threshold_sensitivity(cands, cfg, base, deltas=(0.5,), gates=["oos_sharpe"])
        # min_* を締める (+50%) と通過率は上がらない
        assert rows[0].metrics["gate_pass_rate_perturbed"] <= rows[0].metrics["gate_pass_rate_base"]

    def test_extreme_threshold_flips(self, cands, cfg, prepared):
        _, base = prepared
        rows = M.threshold_sensitivity(cands, cfg, base, deltas=(-1.0,), gates=["complexity"])
        # max_complexity=0 → ほぼ全員 FAIL → 選抜集合が大きく変わる
        assert rows[0].metrics["gate_flip_rate"] > 0.0
        assert rows[0].metrics["topk_jaccard"] < 1.0


class TestAxisAblation:
    def test_one_row_per_weight(self, cands, cfg, prepared):
        bundles, base = prepared
        rows = M.axis_ablation(cands, bundles, cfg, base)
        assert [r.target for r in rows] == list(M.V1_WEIGHT_FIELDS)
        assert all(r.perturbation == "zero" for r in rows)

    def test_already_zero_weight_is_inert(self, cands, cfg, prepared):
        bundles, base = prepared
        cfg0 = dataclasses.replace(cfg, w_pbo_penalty=0.0)
        b0 = M._decide(cands, M.rescore_bundles(bundles, cfg0), cfg0)
        r = M.axis_ablation(cands, bundles, cfg0, b0, weights=["w_pbo_penalty"])[0]
        assert r.metrics["topk_jaccard"] == 1.0 and r.metrics["kendall_tau"] == pytest.approx(1.0)

    def test_dominant_axis_changes_ranking(self, cands, cfg, prepared):
        bundles, base = prepared
        r = M.axis_ablation(cands, bundles, cfg, base, weights=["w_predictive"])[0]
        assert r.metrics["kendall_tau"] < 1.0


class TestWeightPerturbation:
    def test_deterministic_with_seed(self, cands, cfg, prepared):
        bundles, base = prepared
        a = M.weight_perturbation(cands, bundles, cfg, base, n_runs=5, seed=1)[0].metrics
        b = M.weight_perturbation(cands, bundles, cfg, base, n_runs=5, seed=1)[0].metrics
        assert a == b

    def test_zero_noise_is_perfectly_stable(self, cands, cfg, prepared):
        bundles, base = prepared
        m = M.weight_perturbation(cands, bundles, cfg, base, n_runs=3, noise=0.0)[0].metrics
        assert m["kendall_tau_dist"]["min"] == pytest.approx(1.0)
        assert m["topk_jaccard_dist"]["min"] == 1.0
        assert m["decision_flip_rate"] == 0.0

    def test_distribution_fields(self, cands, cfg, prepared):
        bundles, base = prepared
        m = M.weight_perturbation(cands, bundles, cfg, base, n_runs=4)[0].metrics
        assert m["n_runs"] == 4 and m["kendall_tau_dist"]["n"] == 4
        assert -1.0 <= m["kendall_tau"] <= 1.0


# ── レポート / ハッシュ ────────────────────────────────────────────────

class TestReport:
    @pytest.fixture(scope="class")
    def report(self, cands, cfg):
        return M.run_meta_validation(cands, cfg, n_runs=3, deltas=(-0.1, 0.1))

    def test_row_counts(self, report):
        assert len(report.rows_of(M.ANALYSIS_THRESHOLD)) == 16
        assert len(report.rows_of(M.ANALYSIS_ABLATION)) == 10
        assert len(report.rows_of(M.ANALYSIS_WEIGHT)) == 1

    def test_hashes(self, report, cands):
        assert report.policy_hash.startswith("sha256:")
        assert report.dataset_hash == M.dataset_hash(list(reversed(cands)))  # 順序非依存

    def test_dataset_hash_changes_with_input(self, cands):
        c2 = [dataclasses.replace(c) for c in cands]
        c2[0] = dataclasses.replace(c2[0], complexity_score=0.99)
        assert M.dataset_hash(c2) != M.dataset_hash(cands)

    def test_baseline_binding_flag(self, report):
        bl = report.baseline
        assert bl["top_k_binding"] == (bl["n_gate_passed"] > bl["top_k"])

    def test_markdown_sections(self, report):
        md = M.render_markdown(report)
        for s in ("## 1. 閾値感度", "## 2. 軸 ablation", "## 3. 重み摂動安定性", report.policy_hash):
            assert s in md

    def test_non_binding_warning(self, cands):
        rep = M.run_meta_validation(cands, FrostConfig(top_k=60, promotion_top_k=5),
                                    n_runs=1, analyses=(M.ANALYSIS_ABLATION,))
        assert "top_k が非拘束" in M.render_markdown(rep)

    def test_to_dict_json_serializable(self, report):
        json.dumps(report.to_dict(), default=str)

    def test_analyses_subset(self, cands, cfg):
        rep = M.run_meta_validation(cands, cfg, analyses=(M.ANALYSIS_ABLATION,))
        assert {r.analysis_type for r in rep.rows} == {M.ANALYSIS_ABLATION}


# ── DB ブリッジ (MagicMock) ────────────────────────────────────────────

def _conn(table_exists=True):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = ("frost_meta_validation",) if table_exists else (None,)
    return conn, cur


class TestBridge:
    @pytest.fixture(scope="class")
    def report(self, cands, cfg):
        return M.run_meta_validation(cands, cfg, n_runs=2, deltas=(0.1,))

    def test_meta_run_id_deterministic(self, report):
        assert B.make_meta_run_id(report) == B.make_meta_run_id(report)
        assert B.make_meta_run_id(report).startswith("meta:")

    def test_meta_run_id_depends_on_params(self, report):
        r2 = dataclasses.replace(report, params={**report.params, "seed": 1})
        assert B.make_meta_run_id(r2) != B.make_meta_run_id(report)

    def test_insert_all_rows(self, report):
        conn, cur = _conn()
        n = B.insert_meta_validation(conn, report)
        assert n == len(report.rows)
        inserts = [c for c in cur.execute.call_args_list if "INSERT INTO frost_meta_validation" in c.args[0]]
        assert len(inserts) == len(report.rows)
        assert "ON CONFLICT" in inserts[0].args[0]

    def test_skip_when_table_missing(self, report):
        conn, cur = _conn(table_exists=False)
        assert B.insert_meta_validation(conn, report) == 0
        assert not any("INSERT" in c.args[0] for c in cur.execute.call_args_list)

    def test_nan_sanitized(self):
        assert B._num(float("nan")) is None and B._num(None) is None and B._num("0.5") == 0.5
        assert json.loads(B._json({"a": float("inf"), "b": [float("nan"), 1.0]})) == {"a": None, "b": [None, 1.0]}


class TestMigration:
    def test_085_exists_and_columns(self):
        from pathlib import Path
        sql = (Path(__file__).parents[2] / "qedschema/migrations/085_frost_meta_validation.sql").read_text()
        for col in ("meta_run_id", "policy_hash", "dataset_hash", "analysis_type", "decision_flip_rate",
                    "gate_flip_rate", "topk_jaccard", "promo_jaccard", "kendall_tau", "kendall_tau_passed"):
            assert col in sql
        assert "IF NOT EXISTS" in sql


class TestCli:
    def test_cli_synthetic(self, tmp_path):
        import importlib.util
        from pathlib import Path
        p = Path(__file__).parents[2] / "scripts/frost/run_frost_meta_validation.py"
        spec = importlib.util.spec_from_file_location("rfmv", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = tmp_path / "r.md"
        js = tmp_path / "r.json"
        rc = mod.main(["--synthetic", "30", "--runs", "2", "--deltas", "0.1",
                       "--out", str(out), "--json-out", str(js)])
        assert rc == 0 and "## 2. 軸 ablation" in out.read_text()
        assert json.loads(js.read_text())["n_candidates"] == 30

    def test_cli_candidates_json(self, tmp_path):
        import importlib.util
        from pathlib import Path
        p = Path(__file__).parents[2] / "scripts/frost/run_frost_meta_validation.py"
        spec = importlib.util.spec_from_file_location("rfmv2", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cs = make_synthetic_candidates(n=15)
        f = tmp_path / "c.json"
        f.write_text(json.dumps([dataclasses.asdict(c) for c in cs], default=str))
        assert len(mod.load_candidates_json(str(f))) == 15
        assert mod.main(["--candidates-json", str(f), "--runs", "1", "--analyses", "axis_ablation",
                         "--out", str(tmp_path / "o.md")]) == 0
