"""
frost_meta_sensitivity.py
-------------------------
Phase 8 (P8) メタ検証: FROST 選抜器自身の頑健性を測る。

  「バックテストは仮説検証の道具」(ProStock 設計憲法) を選抜器自身に適用する。

3 つの分析:
  1. 閾値感度 (threshold sensitivity)
     各 Hard Gate 閾値を ±10% / ±20% 摂動して全評価をリプレイし、
     - decision_flip_rate : 決定 (SELECTED/HOLD/REJECTED/REVIEW_REQUIRED) が変わった候補の割合
     - gate_flip_rate     : 当該ゲートの PASS/FAIL が変わった候補の割合
     - topk_jaccard       : SELECTED 集合の Jaccard (基準 vs 摂動)
     を算出する。反転率が高いゲート = 閾値が決定を支配している脆弱箇所。
     ※ min_oos_sharpe / max_turnover / max_drawdown はスコアのペナルティ正規化にも
       使われるため、ゲートだけでなく評価全体をリプレイする。

  2. 軸 ablation
     v1 スコアの重みを 1 軸ずつ 0 にして再スコア → SELECTED 集合の Jaccard と Kendall τ。
     Jaccard ≈ 1 の軸は選抜に寄与していない (削除候補, A1 の検証)。

  3. 重み摂動安定性
     全重みに独立に U(-20%, +20%) の乗法ノイズ × N 回 → Kendall τ の分布。
     τ が低ければ線形加重和そのものが信頼できない (A3 の検証)。

設計原則:
  - pure Python (ADR-001: statistics / scipy 不使用)。乱数は random.Random(seed) で決定論的
  - 副作用なし: 入力候補・config を変更しない (config は dataclasses.replace で複製)
  - 重みはスコアにしか効かないため、ablation / 摂動は「証拠を 1 回だけ計算し再スコア」する。
    再スコア結果がフルリプレイと一致することは単体テストで保証する
  - 本モジュールは観測のみ。結果に基づくポリシー変更は本計画スコープ外 (新 Note として登録)
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from analytics.python.frost.evidence_bundle import (
    EvidenceBundle,
    evaluate_candidate_to_bundle,
    evaluation_from_bundle,
)
from analytics.python.frost.frost_decision_engine import apply_final_policy
from analytics.python.frost.frost_ranker import assign_decisions
from analytics.python.frost.score_engine import ScoreEngine

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: ゲート名 → (config フィールド名, [0,1] にクリップするか)
GATE_THRESHOLD_FIELDS: Dict[str, Tuple[str, bool]] = {
    "pbo":                 ("pbo_threshold",           True),
    "rank_ic":             ("min_rank_ic",             False),
    "oos_sharpe":          ("min_oos_sharpe",          False),
    "turnover":            ("max_turnover",            False),
    "max_drawdown":        ("max_drawdown",            True),
    "regime_pass_ratio":   ("min_regime_pass_ratio",   True),
    "complexity":          ("max_complexity_score",    True),
    "selection_stability": ("min_selection_stability", True),
}

#: v1 スコアで実際に使われる重み (ScoreEngine._get_v1_weights と一致させる)
V1_WEIGHT_FIELDS: Tuple[str, ...] = (
    "w_predictive",
    "w_oos_sharpe",
    "w_regime_stability",
    "w_selection_consistency",
    "w_capacity",
    "w_pbo_penalty",
    "w_turnover_penalty",
    "w_complexity_penalty",
    "w_drawdown_penalty",
    "w_fragility_penalty",
)

DEFAULT_THRESHOLD_DELTAS: Tuple[float, ...] = (-0.20, -0.10, 0.10, 0.20)
DEFAULT_WEIGHT_NOISE: float = 0.20
DEFAULT_PERTURBATION_RUNS: int = 50

ANALYSIS_THRESHOLD = "threshold_sensitivity"
ANALYSIS_ABLATION = "axis_ablation"
ANALYSIS_WEIGHT = "weight_perturbation"

SELECTED = "SELECTED"


# ---------------------------------------------------------------------------
# 統計ユーティリティ (pure Python)
# ---------------------------------------------------------------------------

def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """集合 Jaccard 係数。両方空なら 1.0 (変化なし)。"""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def kendall_tau_b(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """
    Kendall の τ-b (同順位補正付き)。O(n²)、pure Python。

    片方が全て同値など分母 0 の場合は None を返す (定義不能)。
    """
    n = len(x)
    if n != len(y):
        raise ValueError("x と y の長さが一致しません")
    if n < 2:
        return None
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n - 1):
        xi, yi = x[i], y[i]
        for j in range(i + 1, n):
            dx = xi - x[j]
            dy = yi - y[j]
            if dx == 0 and dy == 0:
                continue  # 両方同順位: τ-b ではどちらにも数えない
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1
    denom = math.sqrt((concordant + discordant + ties_x) * (concordant + discordant + ties_y))
    if denom == 0:
        return None
    return (concordant - discordant) / denom


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    """線形補間の分位点 (sorted_vals は昇順・非空)。"""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def summarize_distribution(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """min / p05 / p50 / mean / max を返す。空なら全て None。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "min": None, "p05": None, "p50": None, "mean": None, "max": None}
    return {
        "n": len(vals),
        "min": vals[0],
        "p05": _quantile(vals, 0.05),
        "p50": _quantile(vals, 0.50),
        "mean": sum(vals) / len(vals),
        "max": vals[-1],
    }


# ---------------------------------------------------------------------------
# リプレイ
# ---------------------------------------------------------------------------

@dataclass
class ReplaySnapshot:
    """1 回のリプレイ結果 (候補 ID ごとの決定・スコア・ゲート)。"""
    decisions: Dict[str, str]
    scores: Dict[str, float]
    gate_pass: Dict[str, Dict[str, bool]]
    """candidate_id → {gate_name: passed}"""

    passed: List[str] = field(default_factory=list)
    """Hard Gate 全通過の candidate_id。"""

    @property
    def selected(self) -> List[str]:
        return sorted(cid for cid, d in self.decisions.items() if d == SELECTED)

    def top_n_passed(self, n: int) -> List[str]:
        """Gate 通過候補のうちスコア上位 n 件 (同点は candidate_id で決定論化)。"""
        ranked = sorted(self.passed, key=lambda c: (-self.scores.get(c, 0.0), c))
        return ranked[:n]


def _gate_flags(bundle: EvidenceBundle) -> Dict[str, bool]:
    g = bundle.gate
    return {name: bool(getattr(g, f"gate_{name}", True)) for name in GATE_THRESHOLD_FIELDS}


def _decide(candidates: Sequence[Any], bundles: Sequence[EvidenceBundle], config: Any) -> ReplaySnapshot:
    use_v2 = bool(getattr(config, "use_v2_score", False))
    evaluations = [evaluation_from_bundle(b, use_v2_score=use_v2) for b in bundles]
    raw = assign_decisions(list(candidates), evaluations, config)
    decisions, _ = apply_final_policy(raw, evaluations, config)
    return ReplaySnapshot(
        decisions={d.candidate_id: d.decision for d in decisions},
        scores={ev.candidate_id: float(ev.frost_score) for ev in evaluations},
        gate_pass={b.candidate_id: _gate_flags(b) for b in bundles},
        passed=sorted(b.candidate_id for b in bundles if b.gate.passed),
    )


def compute_bundles(candidates: Sequence[Any], config: Any, run_id: str = "meta", trace_id: str = "meta") -> List[EvidenceBundle]:
    """全候補の証拠を計算する (候補は変更しない)。"""
    return [evaluate_candidate_to_bundle(c, run_id, trace_id, config) for c in candidates]


def full_replay(candidates: Sequence[Any], config: Any) -> ReplaySnapshot:
    """評価 → 決定までのフルリプレイ。"""
    return _decide(candidates, compute_bundles(candidates, config), config)


def rescore_bundles(bundles: Sequence[EvidenceBundle], config: Any) -> List[EvidenceBundle]:
    """
    証拠 (特徴量・PBO・安定性・ゲート) を再利用し、スコアのみ config の重みで再計算する。

    重みはスコアにしか影響しないため、フルリプレイと厳密に一致する。
    入力 bundle は変更しない。
    """
    engine = ScoreEngine.from_config(config)
    use_v2 = bool(getattr(config, "use_v2_score", False))
    out = []
    for b in bundles:
        scores = dataclasses.replace(b.scores)
        engine.fill_scores(scores, use_v2=use_v2)
        out.append(dataclasses.replace(b, scores=scores))
    return out


def weight_replay(candidates: Sequence[Any], bundles: Sequence[EvidenceBundle], config: Any) -> ReplaySnapshot:
    """重み変更のみのリプレイ (証拠キャッシュ利用)。"""
    return _decide(candidates, rescore_bundles(bundles, config), config)


# ---------------------------------------------------------------------------
# 比較
# ---------------------------------------------------------------------------

def compare_snapshots(
    base: ReplaySnapshot,
    other: ReplaySnapshot,
    gate: Optional[str] = None,
    promotion_top_k: int = 5,
) -> Dict[str, Any]:
    """
    基準と摂動後のリプレイを比較する。

    - topk_jaccard        : SELECTED 集合の Jaccard
    - promo_jaccard       : Gate 通過候補のスコア上位 promotion_top_k の Jaccard
                            (Gate 通過数 <= top_k のとき SELECTED は Gate だけで決まるため、
                             重みの効果はこちらで測る)
    - kendall_tau         : 全候補のスコア順位相関
    - kendall_tau_passed  : 基準・摂動の両方で Gate 通過した候補に限った順位相関
    """
    cids = sorted(base.decisions)
    n = len(cids)
    flips = [cid for cid in cids if base.decisions[cid] != other.decisions.get(cid)]
    result: Dict[str, Any] = {
        "n_candidates": n,
        "decision_flip_rate": (len(flips) / n) if n else 0.0,
        "n_decision_flips": len(flips),
        "topk_jaccard": jaccard(base.selected, other.selected),
        "n_selected_base": len(base.selected),
        "n_selected_perturbed": len(other.selected),
        "kendall_tau": kendall_tau_b(
            [base.scores.get(c, 0.0) for c in cids],
            [other.scores.get(c, 0.0) for c in cids],
        ),
        "promo_jaccard": jaccard(base.top_n_passed(promotion_top_k), other.top_n_passed(promotion_top_k)),
    }
    both = sorted(set(base.passed) & set(other.passed))
    result["kendall_tau_passed"] = kendall_tau_b(
        [base.scores.get(c, 0.0) for c in both],
        [other.scores.get(c, 0.0) for c in both],
    )
    if gate is not None:
        gflips = sum(
            1 for c in cids
            if base.gate_pass.get(c, {}).get(gate, True) != other.gate_pass.get(c, {}).get(gate, True)
        )
        result["gate_flip_rate"] = (gflips / n) if n else 0.0
        result["gate_pass_rate_base"] = (
            sum(1 for c in cids if base.gate_pass.get(c, {}).get(gate, True)) / n if n else 0.0
        )
        result["gate_pass_rate_perturbed"] = (
            sum(1 for c in cids if other.gate_pass.get(c, {}).get(gate, True)) / n if n else 0.0
        )
    return result


# ---------------------------------------------------------------------------
# 結果型
# ---------------------------------------------------------------------------

@dataclass
class MetaValidationRow:
    """frost_meta_validation テーブルの 1 行に対応する。"""
    analysis_type: str
    target: str
    perturbation: str
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "analysis_type": self.analysis_type,
            "target": self.target,
            "perturbation": self.perturbation,
            **self.metrics,
        }


@dataclass
class MetaValidationReport:
    policy_hash: str
    dataset_hash: str
    n_candidates: int
    baseline: Dict[str, Any]
    rows: List[MetaValidationRow] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)

    def rows_of(self, analysis_type: str) -> List[MetaValidationRow]:
        return [r for r in self.rows if r.analysis_type == analysis_type]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "dataset_hash": self.dataset_hash,
            "n_candidates": self.n_candidates,
            "baseline": self.baseline,
            "params": self.params,
            "rows": [r.to_dict() for r in self.rows],
        }


# ---------------------------------------------------------------------------
# 分析本体
# ---------------------------------------------------------------------------

def perturb_threshold(config: Any, field_name: str, delta: float, clip01: bool) -> Any:
    """閾値を (1 + delta) 倍した config 複製を返す。"""
    val = float(getattr(config, field_name)) * (1.0 + delta)
    if clip01:
        val = min(1.0, max(0.0, val))
    return dataclasses.replace(config, **{field_name: val})


def threshold_sensitivity(
    candidates: Sequence[Any],
    config: Any,
    base: ReplaySnapshot,
    deltas: Sequence[float] = DEFAULT_THRESHOLD_DELTAS,
    gates: Optional[Sequence[str]] = None,
) -> List[MetaValidationRow]:
    rows = []
    for gate in (gates or list(GATE_THRESHOLD_FIELDS)):
        field_name, clip01 = GATE_THRESHOLD_FIELDS[gate]
        for delta in deltas:
            cfg = perturb_threshold(config, field_name, delta, clip01)
            snap = full_replay(candidates, cfg)
            m = compare_snapshots(base, snap, gate=gate, promotion_top_k=config.promotion_top_k)
            m["field"] = field_name
            m["base_value"] = float(getattr(config, field_name))
            m["perturbed_value"] = float(getattr(cfg, field_name))
            rows.append(MetaValidationRow(ANALYSIS_THRESHOLD, gate, f"{delta:+.0%}", m))
    return rows


def axis_ablation(
    candidates: Sequence[Any],
    bundles: Sequence[EvidenceBundle],
    config: Any,
    base: ReplaySnapshot,
    weights: Sequence[str] = V1_WEIGHT_FIELDS,
) -> List[MetaValidationRow]:
    rows = []
    for w in weights:
        cfg = dataclasses.replace(config, **{w: 0.0})
        snap = weight_replay(candidates, bundles, cfg)
        m = compare_snapshots(base, snap, promotion_top_k=config.promotion_top_k)
        m["base_weight"] = float(getattr(config, w))
        rows.append(MetaValidationRow(ANALYSIS_ABLATION, w, "zero", m))
    return rows


def weight_perturbation(
    candidates: Sequence[Any],
    bundles: Sequence[EvidenceBundle],
    config: Any,
    base: ReplaySnapshot,
    n_runs: int = DEFAULT_PERTURBATION_RUNS,
    noise: float = DEFAULT_WEIGHT_NOISE,
    seed: int = 20261003,
    weights: Sequence[str] = V1_WEIGHT_FIELDS,
) -> List[MetaValidationRow]:
    """全重みに乗法ノイズ U(-noise, +noise) を N 回。分布サマリ行 1 行を返す。"""
    rng = random.Random(seed)
    taus: List[float] = []
    taus_p: List[float] = []
    jacs: List[float] = []
    promos: List[float] = []
    flips: List[float] = []
    for _ in range(n_runs):
        override = {w: float(getattr(config, w)) * (1.0 + rng.uniform(-noise, noise)) for w in weights}
        snap = weight_replay(candidates, bundles, dataclasses.replace(config, **override))
        m = compare_snapshots(base, snap, promotion_top_k=config.promotion_top_k)
        if m["kendall_tau"] is not None:
            taus.append(m["kendall_tau"])
        if m["kendall_tau_passed"] is not None:
            taus_p.append(m["kendall_tau_passed"])
        jacs.append(m["topk_jaccard"])
        promos.append(m["promo_jaccard"])
        flips.append(m["decision_flip_rate"])
    metrics = {
        "n_candidates": len(base.decisions),
        "n_runs": n_runs,
        "noise": noise,
        "seed": seed,
        "kendall_tau": summarize_distribution(taus)["p50"],
        "kendall_tau_dist": summarize_distribution(taus),
        "kendall_tau_passed": summarize_distribution(taus_p)["p50"],
        "kendall_tau_passed_dist": summarize_distribution(taus_p),
        "promo_jaccard": summarize_distribution(promos)["p50"],
        "promo_jaccard_dist": summarize_distribution(promos),
        "topk_jaccard": summarize_distribution(jacs)["p50"],
        "topk_jaccard_dist": summarize_distribution(jacs),
        "decision_flip_rate": summarize_distribution(flips)["mean"],
        "decision_flip_rate_dist": summarize_distribution(flips),
    }
    return [MetaValidationRow(ANALYSIS_WEIGHT, "all_v1_weights", f"±{noise:.0%}x{n_runs}", metrics)]


def dataset_hash(candidates: Sequence[Any]) -> str:
    """候補入力 (評価に使うフィールドのみ) の決定論的ハッシュ。"""
    keys = ("candidate_id", "candidate_hash", "source_candidate_id", "formula_text", "complexity_score",
            "horizon", "backtest_summary", "metrics", "regime_breakdown", "fold_results")
    payload = [{k: getattr(c, k, None) for k in keys} for c in candidates]
    payload.sort(key=lambda d: str(d["candidate_id"]))
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _policy_hash(config: Any) -> str:
    try:
        from analytics.python.frost.policy_spec import policy_spec_from_frost_config
        return policy_spec_from_frost_config(config).policy_hash
    except Exception:  # PolicySpec を直接渡された場合など
        return str(getattr(config, "policy_hash", ""))


def run_meta_validation(
    candidates: Sequence[Any],
    config: Any,
    deltas: Sequence[float] = DEFAULT_THRESHOLD_DELTAS,
    n_runs: int = DEFAULT_PERTURBATION_RUNS,
    noise: float = DEFAULT_WEIGHT_NOISE,
    seed: int = 20261003,
    analyses: Sequence[str] = (ANALYSIS_THRESHOLD, ANALYSIS_ABLATION, ANALYSIS_WEIGHT),
) -> MetaValidationReport:
    """3 分析をまとめて実行する。"""
    bundles = compute_bundles(candidates, config)
    base = _decide(candidates, bundles, config)
    counts: Dict[str, int] = {}
    for d in base.decisions.values():
        counts[d] = counts.get(d, 0) + 1
    report = MetaValidationReport(
        policy_hash=_policy_hash(config),
        dataset_hash=dataset_hash(candidates),
        n_candidates=len(candidates),
        baseline={"decision_counts": counts, "n_selected": len(base.selected),
                  "n_gate_passed": len(base.passed),
                  "top_k": int(config.top_k),
                  "promotion_top_k": int(config.promotion_top_k),
                  # Gate 通過数 > top_k のときだけ重み (スコア) が SELECTED 集合を左右する
                  "top_k_binding": len(base.passed) > int(config.top_k),
                  "gate_pass_rates": {
                      g: (sum(1 for f in base.gate_pass.values() if f.get(g, True)) / len(candidates)
                          if candidates else 0.0)
                      for g in GATE_THRESHOLD_FIELDS}},
        params={"deltas": list(deltas), "n_runs": n_runs, "noise": noise, "seed": seed,
                "use_v2_score": bool(getattr(config, "use_v2_score", False))},
    )
    if ANALYSIS_THRESHOLD in analyses:
        report.rows += threshold_sensitivity(candidates, config, base, deltas)
    if ANALYSIS_ABLATION in analyses:
        report.rows += axis_ablation(candidates, bundles, config, base)
    if ANALYSIS_WEIGHT in analyses:
        report.rows += weight_perturbation(candidates, bundles, config, base, n_runs, noise, seed)
    return report


# ---------------------------------------------------------------------------
# Markdown レポート
# ---------------------------------------------------------------------------

def _f(v: Any, nd: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(report: MetaValidationReport, title: str = "FROST メタ検証レポート (P8)") -> str:
    L: List[str] = [f"# {title}", ""]
    L += [f"- policy_hash: `{report.policy_hash}`",
          f"- dataset_hash: `{report.dataset_hash}`",
          f"- 候補数: {report.n_candidates}",
          f"- 基準決定: {report.baseline.get('decision_counts')}",
          f"- Gate 通過: {report.baseline.get('n_gate_passed')} / top_k={report.baseline.get('top_k')} "
          f"(promotion_top_k={report.baseline.get('promotion_top_k')})",
          f"- パラメータ: {report.params}", ""]
    if report.baseline.get("top_k_binding") is False:
        L += ["> ⚠ **top_k が非拘束**: Gate 通過数 <= top_k のため SELECTED 集合は Gate だけで決まり、"
              "スコア重みは選抜集合に影響しない。重みの効果は promo_jaccard (上位 promotion_top_k) と "
              "kendall_tau_passed で評価すること。", ""]
    L += ["> 本レポートは観測結果のみ。ポリシー変更は新 Note として登録し通常の検証サイクルへ回すこと。", ""]

    th = report.rows_of(ANALYSIS_THRESHOLD)
    if th:
        L += ["## 1. 閾値感度", "",
              "| gate | 摂動 | 閾値 | gate反転率 | 決定反転率 | TOP_K Jaccard |",
              "|---|---|---|---|---|---|"]
        for r in th:
            m = r.metrics
            L.append(f"| {r.target} | {r.perturbation} | {_f(m.get('base_value'))}→{_f(m.get('perturbed_value'))} | "
                     f"{_f(m.get('gate_flip_rate'))} | {_f(m.get('decision_flip_rate'))} | {_f(m.get('topk_jaccard'))} |")
        # ゲート別最大反転率
        worst: Dict[str, float] = {}
        for r in th:
            worst[r.target] = max(worst.get(r.target, 0.0), r.metrics.get("decision_flip_rate", 0.0))
        L += ["", "**ゲート別 最大決定反転率 (降順)**: " +
              ", ".join(f"{g}={v:.3f}" for g, v in sorted(worst.items(), key=lambda kv: -kv[1])), ""]

    ab = report.rows_of(ANALYSIS_ABLATION)
    if ab:
        L += ["## 2. 軸 ablation (重み 0 化)", "",
              "| 軸 | 元重み | TOP_K Jaccard | 昇格上位 Jaccard | τ (Gate通過) | τ (全体) | 決定反転率 |",
              "|---|---|---|---|---|---|---|"]
        for r in sorted(ab, key=lambda r: (r.metrics.get("promo_jaccard", 1.0), r.metrics.get("topk_jaccard", 1.0))):
            m = r.metrics
            L.append(f"| {r.target} | {_f(m.get('base_weight'))} | {_f(m.get('topk_jaccard'))} | "
                     f"{_f(m.get('promo_jaccard'))} | {_f(m.get('kendall_tau_passed'))} | "
                     f"{_f(m.get('kendall_tau'))} | {_f(m.get('decision_flip_rate'))} |")
        inert = [r.target for r in ab
                 if r.metrics.get("topk_jaccard") == 1.0 and r.metrics.get("promo_jaccard") == 1.0]
        L += ["", f"**除外しても選抜不変の軸 (削除候補)**: {', '.join(inert) if inert else 'なし'}", ""]

    wp = report.rows_of(ANALYSIS_WEIGHT)
    if wp:
        L += ["## 3. 重み摂動安定性", ""]
        for r in wp:
            m = r.metrics
            td, jd = m.get("kendall_tau_dist", {}), m.get("topk_jaccard_dist", {})
            tp, pj = m.get("kendall_tau_passed_dist", {}), m.get("promo_jaccard_dist", {})
            L += [f"- 条件: {r.perturbation} (seed={m.get('seed')})",
                  f"- Kendall τ: min={_f(td.get('min'))} p05={_f(td.get('p05'))} p50={_f(td.get('p50'))} mean={_f(td.get('mean'))}",
                  f"- Kendall τ (Gate 通過のみ): min={_f(tp.get('min'))} p05={_f(tp.get('p05'))} p50={_f(tp.get('p50'))}",
                  f"- TOP_K Jaccard: min={_f(jd.get('min'))} p05={_f(jd.get('p05'))} p50={_f(jd.get('p50'))}",
                  f"- 昇格上位 Jaccard: min={_f(pj.get('min'))} p05={_f(pj.get('p05'))} p50={_f(pj.get('p50'))} mean={_f(pj.get('mean'))}",
                  f"- 平均決定反転率: {_f(m.get('decision_flip_rate'))}", ""]
    return "\n".join(L) + "\n"
