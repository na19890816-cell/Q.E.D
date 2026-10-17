"""
eml_search.py
-------------
EML アルファ候補探索エンジン。

2つの探索モード:
1. exhaustive_search : depth <= max_depth の全木を列挙して評価
2. gradient_search   : Adam soft-training → temperature annealing → snap

両モードとも EMLCandidate リストを返す。

ADR-002 (系譜ログ): 両関数は任意引数 stats_out (list) を受け取り、指定時は
top_k で切り捨てる前の試行数・fitness 統計を SearchStats として追記する。
戻り値・既存引数は不変 (golden 非影響)。
"""
from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import pandas as pd

from .eml_core import (
    EML_DEPTH_MAX,
    EML_DEPTH_MIN,
    EMLNode,
    validate_depth,
)
from .eml_compiler import compile_to_expr
from .eml_tree import (
    enumerate_trees,
    random_tree,
    snap_weights,
    copy_tree,
)
from analytics.python.audit.exploration_events import AuditError, evidence_hash


# ------------------------------------------------------------------ #
# データ構造
# ------------------------------------------------------------------ #

@dataclass
class EMLCandidate:
    """探索で生成された候補アルファ。"""
    candidate_id: str
    run_id: str
    trace_id: str
    node: EMLNode
    compiled_expr: str          # compile_to_expr() 結果
    fitness_score: float = 0.0
    status: str = "candidate"   # candidate / promoted / rejected
    rejection_reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def tree_depth(self) -> int:
        return self.node.depth()

    def node_count(self) -> int:
        return self.node.node_count()


@dataclass(frozen=True)
class SearchStats:
    """
    1 回の探索呼び出しの試行統計 (ADR-002 S1)。

    n_evaluated は fitness を計算した木の数 (top_k 切り捨て前、例外で -999 になったものも含む)。
    n_distinct_expr は評価した木の compiled_expr の異なり数 = 実際に検定した異なるシグナル数。

    ADR-002 §4.2 の「試行」は n_distinct_expr (同一式の再評価は同一の検定であり多重比較を増やさない)。
    exhaustive は EML セレクタの初期重み (raw_weight=0 → 左選択) により大量の木が同一式に
    縮退するため、n_evaluated を N に使うと数千倍の過大計上になる。
    """

    @property
    def n_trials(self) -> int:
        """DSR 用の試行数。n_distinct_expr が未設定 (0) の旧データは n_evaluated にフォールバック。"""
        return self.n_distinct_expr if self.n_distinct_expr > 0 else self.n_evaluated
    stage: str                  # "exhaustive" | "gradient"
    n_generated: int            # 生成した木の数
    n_evaluated: int            # fitness を計算した数 (= 試行数)
    n_invalid: int              # depth 検証で除外した数 (未評価)
    n_fitness_failed: int       # fitness 例外 (-999 扱い) の数
    n_returned: int             # top_k 後に返した数
    fitness_count: int = 0      # 有限 fitness の数 (失敗・NaN 除く)
    fitness_mean: float = 0.0
    fitness_m2: float = 0.0     # Σ(x - mean)^2
    fitness_evals: int = 0      # 内部の fitness 呼び出し総数 (gradient の有限差分含む)
    fitness_kind: str = "rank_ic"
    n_fitness_nonfinite: int = 0  # NaN / Inf を返した数 (試行数には含む)
    n_distinct_expr: int = 0      # 評価候補の compiled_expr 異なり数 (= DSR の試行数)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["n_trials"] = self.n_trials
        return d


_FITNESS_FAIL = -999.0


def _n_nonfinite(scores: List[float]) -> int:
    return sum(1 for x in scores if not math.isfinite(x))


def _fitness_moments(scores: List[float]):
    xs = [x for x in scores if math.isfinite(x) and x != _FITNESS_FAIL]
    n = len(xs)
    if n == 0:
        return 0, 0.0, 0.0
    mean = math.fsum(xs) / n
    return n, mean, math.fsum((x - mean) ** 2 for x in xs)


# ------------------------------------------------------------------ #
# 探索エンジン
# ------------------------------------------------------------------ #

FitnessFunc = Callable[[EMLNode, pd.DataFrame, pd.Series], float]


def exhaustive_search(
    terminals: List[str],
    max_depth: int,
    run_id: str,
    trace_id: str,
    feature_df: pd.DataFrame,
    target: pd.Series,
    fitness_fn: FitnessFunc,
    top_k: int = 20,
    stats_out: Optional[list] = None,
    audit=None,
) -> List[EMLCandidate]:
    """
    depth <= max_depth の全 EML 木を列挙し、上位 top_k を返す。
    max_depth は EML_DEPTH_MAX(=4) にクランプ。

    stats_out が与えられた場合、SearchStats を 1 件追記する (ADR-002)。
    """
    max_depth = min(max(max_depth, EML_DEPTH_MIN), EML_DEPTH_MAX)
    scope = None
    if audit is not None:
        audit.require_run(run_id)
        scope = audit.start_scope("eml_exhaustive", {"terminals": terminals, "max_depth": max_depth,
                                                    "top_k": top_k, "fitness_kind": "caller_supplied"})
    trees = enumerate_trees(max_depth, terminals)

    candidates: List[EMLCandidate] = []
    n_invalid = 0
    n_failed = 0
    for index, tree in enumerate(trees):
        trial = f"{scope}/{index}"
        if audit is not None:
            from analytics.python.audit.exploration_events import audit_value
            audit.start_trial(scope, trial, evidence={"tree": audit_value(tree), "tree_sha256": evidence_hash(tree), "index": index},
                              counting_owner="ADR002_S1", n_raw_role="distinct_final_expression_per_stage_run")
        try:
            validate_depth(tree, label="exhaustive_search")
        except ValueError:
            n_invalid += 1
            if audit is not None:
                audit.finish_trial(trial, status="INVALID", result={"reason": "DEPTH_VALIDATION"})
            continue

        expr = compile_to_expr(snap_weights(tree))
        if audit is not None:
            audit.note("EVALUATION_STARTED", {"trial_id": trial, "kind": "final", "formula_key": expr})
        error_type = None
        try:
            score = fitness_fn(tree, feature_df, target)
        except Exception as exc:
            if audit is not None and isinstance(exc, AuditError):
                raise
            error_type = type(exc).__name__
            score = _FITNESS_FAIL
        if score == _FITNESS_FAIL:
            n_failed += 1

        cid = str(uuid.uuid4())
        if audit is not None:
            audit.finish_trial(trial, status="FAILED" if score == _FITNESS_FAIL else
                               "NONFINITE" if not math.isfinite(score) else "SUCCEEDED",
                               result={"candidate_id": cid, "fitness": score, "error_type": error_type},
                               formula_key=expr)
        candidates.append(
            EMLCandidate(
                candidate_id=cid,
                run_id=run_id,
                trace_id=trace_id,
                node=tree,
                compiled_expr=expr,
                fitness_score=score,
                metadata={"search_mode": "exhaustive"},
            )
        )

    # 上位 top_k
    candidates.sort(key=lambda c: c.fitness_score, reverse=True)
    result = candidates[:top_k]
    if audit is not None:
        returned = {c.candidate_id for c in result}
        for c in candidates:
            audit.note("CANDIDATE_DISPOSITION", {"scope_id": scope, "candidate_id": c.candidate_id,
                                                "returned_after_top_k": c.candidate_id in returned})
        audit.finish_scope(scope, {"n_generated": len(trees), "n_evaluated": len(candidates),
                                  "n_invalid": n_invalid, "n_fitness_failed": n_failed,
                                  "n_fitness_nonfinite": _n_nonfinite([c.fitness_score for c in candidates]),
                                  "n_returned": len(result), "n_distinct_expr": len({c.compiled_expr for c in candidates})})

    if stats_out is not None:
        fc, fm, fm2 = _fitness_moments([c.fitness_score for c in candidates])
        stats_out.append(SearchStats(
            stage="exhaustive",
            n_generated=len(trees),
            n_evaluated=len(candidates),
            n_invalid=n_invalid,
            n_fitness_failed=n_failed,
            n_returned=len(result),
            fitness_count=fc, fitness_mean=fm, fitness_m2=fm2,
            fitness_evals=len(candidates),
            n_fitness_nonfinite=_n_nonfinite([c.fitness_score for c in candidates]),
            n_distinct_expr=len({c.compiled_expr for c in candidates}),
        ))
    return result


def gradient_search(
    terminals: List[str],
    max_depth: int,
    run_id: str,
    trace_id: str,
    feature_df: pd.DataFrame,
    target: pd.Series,
    fitness_fn: FitnessFunc,
    n_init: int = 10,
    adam_steps: int = 50,
    lr: float = 0.1,
    temperature_start: float = 2.0,
    temperature_end: float = 0.1,
    top_k: int = 10,
    rng_seed: Optional[int] = None,
    stats_out: Optional[list] = None,
    audit=None,
) -> List[EMLCandidate]:
    """
    勾配ベース探索 (Adam soft-training + temperature annealing + snap)。

    各初期木に対して:
      1. Adam で raw_weight を更新
      2. temperature annealing でソフト重みを収束
      3. snap → compile → fitness 評価

    stats_out が与えられた場合、SearchStats を 1 件追記する (ADR-002)。
    試行数は最終的に snap・評価された木の数 (n_init 相当) とし、Adam 内部の
    有限差分評価は fitness_evals に参考値として記録する (独立試行ではないため)。
    """
    rng = random.Random(rng_seed)
    max_depth = min(max(max_depth, EML_DEPTH_MIN), EML_DEPTH_MAX)
    candidates: List[EMLCandidate] = []
    n_invalid = 0
    n_failed = 0
    fitness_evals = 0
    scope = None
    if audit is not None:
        audit.require_run(run_id)
        scope = audit.start_scope("eml_gradient", {"terminals": terminals, "max_depth": max_depth,
                                "n_init": n_init, "adam_steps": adam_steps, "lr": lr,
                                "temperature_start": temperature_start, "temperature_end": temperature_end,
                                "top_k": top_k, "rng_seed": rng_seed, "fitness_kind": "caller_supplied"})

    for i in range(n_init):
        trial = f"{scope}/{i}"
        if audit is not None:
            audit.start_trial(scope, trial, evidence={"init_index": i}, counting_owner="ADR002_S1",
                              n_raw_role="distinct_final_expression_per_stage_run")
        tree = random_tree(max_depth, terminals, rng=rng)
        observed_fitness = fitness_fn
        if audit is not None:
            from analytics.python.audit.exploration_events import audit_value
            audit.note("CANDIDATE_GENERATED", {"trial_id": trial, "tree": audit_value(tree), "tree_sha256": evidence_hash(tree)})
            probe = [0]

            def observed_fitness(node, features, targets):
                probe[0] += 1
                key = {"trial_id": trial, "kind": "optimizer_probe", "probe_index": probe[0],
                       "tree": audit_value(node), "tree_sha256": evidence_hash(node), "counts_as_independent_trial": False}
                audit.note("EVALUATION_STARTED", key)
                try:
                    score = fitness_fn(node, features, targets)
                except BaseException as exc:
                    audit.note("EVALUATION_FINISHED", {**key, "status": "FAILED", "error_type": type(exc).__name__})
                    raise
                audit.note("EVALUATION_FINISHED", {**key, "fitness": score})
                return score
        trained = _adam_train(
            tree, feature_df, target, observed_fitness,
            steps=adam_steps, lr=lr,
            temp_start=temperature_start, temp_end=temperature_end,
            **({"audit_enabled": True} if audit is not None else {}),
        )
        snapped = snap_weights(trained)
        # Adam: steps × (EML ノード数) × 2 回の有限差分評価
        fitness_evals += adam_steps * len(_collect_eml_nodes(tree)) * 2

        try:
            validate_depth(snapped, label=f"gradient_search init={i}")
        except ValueError:
            n_invalid += 1
            if audit is not None:
                audit.finish_trial(trial, status="INVALID", result={"reason": "DEPTH_VALIDATION"})
            continue

        expr = compile_to_expr(snapped)
        if audit is not None:
            audit.note("EVALUATION_STARTED", {"trial_id": trial, "kind": "final", "formula_key": expr,
                                               "snapped_tree": audit_value(snapped)})
        error_type = None
        try:
            score = fitness_fn(snapped, feature_df, target)
        except Exception as exc:
            if audit is not None and isinstance(exc, AuditError):
                raise
            error_type = type(exc).__name__
            score = _FITNESS_FAIL
        fitness_evals += 1
        if score == _FITNESS_FAIL:
            n_failed += 1

        cid = str(uuid.uuid4())
        if audit is not None:
            audit.finish_trial(trial, status="FAILED" if score == _FITNESS_FAIL else
                               "NONFINITE" if not math.isfinite(score) else "SUCCEEDED",
                               result={"candidate_id": cid, "fitness": score, "error_type": error_type,
                                       "optimizer_probe_count": probe[0]}, formula_key=expr)
        candidates.append(
            EMLCandidate(
                candidate_id=cid,
                run_id=run_id,
                trace_id=trace_id,
                node=snapped,
                compiled_expr=expr,
                fitness_score=score,
                metadata={"search_mode": "gradient", "init_idx": i},
            )
        )

    candidates.sort(key=lambda c: c.fitness_score, reverse=True)
    result = candidates[:top_k]
    if audit is not None:
        returned = {c.candidate_id for c in result}
        for c in candidates:
            audit.note("CANDIDATE_DISPOSITION", {"scope_id": scope, "candidate_id": c.candidate_id,
                                                "returned_after_top_k": c.candidate_id in returned})
        audit.finish_scope(scope, {"n_generated": n_init, "n_evaluated": len(candidates),
                                  "n_invalid": n_invalid, "n_fitness_failed": n_failed,
                                  "n_fitness_nonfinite": _n_nonfinite([c.fitness_score for c in candidates]),
                                  "n_returned": len(result), "n_distinct_expr": len({c.compiled_expr for c in candidates})})

    if stats_out is not None:
        fc, fm, fm2 = _fitness_moments([c.fitness_score for c in candidates])
        stats_out.append(SearchStats(
            stage="gradient",
            n_generated=n_init,
            n_evaluated=len(candidates),
            n_invalid=n_invalid,
            n_fitness_failed=n_failed,
            n_returned=len(result),
            fitness_count=fc, fitness_mean=fm, fitness_m2=fm2,
            fitness_evals=fitness_evals,
            n_fitness_nonfinite=_n_nonfinite([c.fitness_score for c in candidates]),
            n_distinct_expr=len({c.compiled_expr for c in candidates}),
        ))
    return result


def _adam_train(
    node: EMLNode,
    feature_df: pd.DataFrame,
    target: pd.Series,
    fitness_fn: FitnessFunc,
    steps: int,
    lr: float,
    temp_start: float,
    temp_end: float,
    beta1: float = 0.9,
    beta2: float = 0.999,
    eps: float = 1e-8,
    audit_enabled: bool = False,
) -> EMLNode:
    """
    EML 木の raw_weight を Adam で更新。
    勾配は有限差分で近似。
    """
    node = copy_tree(node)
    eml_nodes = _collect_eml_nodes(node)
    if not eml_nodes:
        return node

    # Adam state
    m = [0.0] * len(eml_nodes)
    v = [0.0] * len(eml_nodes)

    for step in range(1, steps + 1):
        temp = temp_start * math.exp(
            math.log(temp_end / temp_start) * step / steps
        )
        delta = 0.01 * temp

        for idx, n in enumerate(eml_nodes):
            orig = n.raw_weight
            # f(w + delta)
            n.raw_weight = orig + delta
            score_p = _safe_fitness(fitness_fn, node, feature_df, target, audit_enabled=audit_enabled)
            # f(w - delta)
            n.raw_weight = orig - delta
            score_m = _safe_fitness(fitness_fn, node, feature_df, target, audit_enabled=audit_enabled)
            # 勾配: 最大化方向
            grad = (score_p - score_m) / (2 * delta)

            # Adam 更新
            m[idx] = beta1 * m[idx] + (1 - beta1) * grad
            v[idx] = beta2 * v[idx] + (1 - beta2) * grad ** 2
            m_hat = m[idx] / (1 - beta1 ** step)
            v_hat = v[idx] / (1 - beta2 ** step)

            n.raw_weight = orig + lr * m_hat / (math.sqrt(v_hat) + eps)

    return node


def _collect_eml_nodes(node: EMLNode) -> List[EMLNode]:
    """EML ノードを再帰収集。"""
    from .eml_core import NODE_EML
    if node.kind != NODE_EML:
        return []
    result = [node]
    if node.left:
        result += _collect_eml_nodes(node.left)
    if node.right:
        result += _collect_eml_nodes(node.right)
    return result


def _safe_fitness(
    fn: FitnessFunc,
    node: EMLNode,
    feature_df: pd.DataFrame,
    target: pd.Series,
    audit_enabled: bool = False,
) -> float:
    try:
        return float(fn(node, feature_df, target))
    except Exception as exc:
        if audit_enabled and isinstance(exc, AuditError):
            raise
        return -999.0
