"""
frost_run_lineage.py
--------------------
ADR-002 S2: FROST 評価結果 (FrostRunOutput) → 試行台帳 (TrialBatch) への変換 (純関数)。

計上規則:
  - **source_type != "eml" の候補のみ計上する** (ADR-002 §10 S2)。
    EML 候補は探索段階 (S1, stage=exhaustive/gradient) で「評価した異なる式」として計上済みで、
    FROST に来るのはその top_k 生存者 = 既計上の式。ここで再計上すると同一式の二重計上になる。
    件数は metadata.excluded_eml_candidates に参考値として残す。
  - 1 family = 1 TrialBatch (stage="frost_eval")。
  - n_trials = 評価済み候補の異なる candidate_hash 数 (S1 と同じ「異なる式」の定義)。
    candidate_hash が空の候補は candidate_id で代替 (過少計上しない側)。
  - sr_stats: FROST は投入された候補を **全件** 評価するため (評価後の生存者選別がない)、
    oos_sharpe の分布は試行間分散の推定に使える。年率 Sharpe を sqrt(annualization) で
    非年率 (daily) へ変換して記録する。annualization<=0 なら sr_stats は空 (保守側フォールバック)。
  - family_key: horizon × universe × target × terminal_set_hash。
    universe / target / terminal_set_hash は候補の feature_spec_json から取り、無ければ既定値
    (EML 既定と同じ panel) に帰属させる。迷ったら計上する (ADR-002 §4.6)。
    既定値で帰属させた件数は metadata.family_source に記録する。
  - batch_id = UUID5(run_id, "frost_eval:<family_key>") — 同じ run の再記録は台帳で重複しない。
"""
from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from analytics.python.frost.frost_lineage import (
    TrialBatch,
    SharpeStats,
    family_spec_dict,
    make_batch_id,
    make_family_key,
)

STAGE = "frost_eval"
EXCLUDED_SOURCE_TYPES: Tuple[str, ...] = ("eml",)

DEFAULT_UNIVERSE = "event_study_panel"   # EMLConfig.universe の既定と一致
DEFAULT_TARGET = "abnormal_return"       # EMLConfig.target_name の既定と一致
DEFAULT_ANNUALIZATION = 252


def ledger_defaults_from_env() -> Dict[str, Any]:
    """環境変数から既定値を読む (FROST_LEDGER_UNIVERSE / _TARGET / _SR_ANNUALIZATION)。"""
    try:
        ann = int(os.environ.get("FROST_LEDGER_SR_ANNUALIZATION", DEFAULT_ANNUALIZATION))
    except ValueError:
        ann = DEFAULT_ANNUALIZATION
    return {
        "default_universe": os.environ.get("FROST_LEDGER_UNIVERSE", DEFAULT_UNIVERSE) or DEFAULT_UNIVERSE,
        "default_target": os.environ.get("FROST_LEDGER_TARGET", DEFAULT_TARGET) or DEFAULT_TARGET,
        "sharpe_annualization": ann,
    }


def _spec_value(spec: Dict[str, Any], *keys: str) -> Optional[str]:
    for k in keys:
        v = spec.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def candidate_family(
    candidate: Any,
    default_universe: str = DEFAULT_UNIVERSE,
    default_target: str = DEFAULT_TARGET,
) -> Tuple[str, Dict[str, Any], bool]:
    """
    候補の (family_key, family_spec, used_default) を返す。

    used_default=True は universe / target のいずれかを既定値で補ったことを示す。
    """
    spec = dict(getattr(candidate, "feature_spec_json", None) or {})
    universe = _spec_value(spec, "universe")
    target = _spec_value(spec, "target_name", "target")
    tsh = _spec_value(spec, "terminal_set_hash") or ""
    horizon = str(getattr(candidate, "horizon", "") or "").strip() or "5d"
    used_default = universe is None or target is None
    universe = universe or default_universe
    target = target or default_target
    return (
        make_family_key(horizon=horizon, universe=universe, target=target, terminal_set_hash=tsh),
        family_spec_dict(horizon, universe, target, tsh),
        used_default,
    )


def _finite(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def trial_batches_from_frost_output(
    output: Any,
    recorded_at: Optional[datetime] = None,
    default_universe: str = DEFAULT_UNIVERSE,
    default_target: str = DEFAULT_TARGET,
    sharpe_annualization: int = DEFAULT_ANNUALIZATION,
    excluded_source_types: Tuple[str, ...] = EXCLUDED_SOURCE_TYPES,
) -> List[TrialBatch]:
    """
    FrostRunOutput を TrialBatch のリスト (family ごと 1 件) に変換する。

    評価結果 (output.evaluations) が無い候補は「試行していない」ので計上しない
    (FROST 無効 / 評価前の例外)。
    """
    run_id = str(getattr(output, "run_id", "") or "")
    if not run_id:
        raise ValueError("trial_batches_from_frost_output: output.run_id が空です")
    ev_by_cid = {ev.candidate_id: ev for ev in (getattr(output, "evaluations", None) or [])}

    groups: Dict[str, Dict[str, Any]] = {}
    excluded: Dict[str, int] = {}
    for c in getattr(output, "candidates", None) or []:
        ev = ev_by_cid.get(c.candidate_id)
        if ev is None:
            continue
        st = str(getattr(c, "source_type", "") or "")
        if st in excluded_source_types:
            excluded[st] = excluded.get(st, 0) + 1
            continue
        fam, spec, used_default = candidate_family(c, default_universe, default_target)
        g = groups.setdefault(fam, {"spec": spec, "hashes": {}, "source_types": {},
                                    "n_default": 0, "n_candidates": 0})
        key = str(getattr(c, "candidate_hash", "") or "") or f"cid:{c.candidate_id}"
        g["n_candidates"] += 1
        g["n_default"] += int(used_default)
        g["source_types"][st] = g["source_types"].get(st, 0) + 1
        # 同一式 (hash) は 1 試行。Sharpe は最初に現れた評価を採用 (決定論: 候補順)
        g["hashes"].setdefault(key, _finite(getattr(ev, "oos_sharpe", None)))

    batches: List[TrialBatch] = []
    for fam in sorted(groups):
        g = groups[fam]
        sharpes_ann = [v for v in g["hashes"].values() if v is not None]
        if sharpe_annualization and sharpe_annualization > 0:
            scale = math.sqrt(sharpe_annualization)
            sr = SharpeStats.from_values([v / scale for v in sharpes_ann])
        else:
            sr = SharpeStats()
        batches.append(TrialBatch(
            batch_id=make_batch_id(run_id, f"{STAGE}:{fam}", 0),
            family_key=fam,
            run_id=run_id,
            stage=STAGE,
            n_trials=len(g["hashes"]),
            sr_stats=sr,
            sr_periodicity="daily",
            trace_id=str(getattr(output, "trace_id", "") or ""),
            source_type="frost",
            family_spec=g["spec"],
            recorded_at=recorded_at,
            metadata={
                "n_candidates_evaluated": g["n_candidates"],
                "n_distinct_hash": len(g["hashes"]),
                "n_oos_sharpe": len(sharpes_ann),
                "source_types": dict(sorted(g["source_types"].items())),
                "family_source": {"default_filled": g["n_default"],
                                  "from_candidate": g["n_candidates"] - g["n_default"]},
                "sharpe_annualization": sharpe_annualization,
                "excluded_eml_candidates": sum(excluded.values()),
                "batch_label": getattr(output, "batch_label", None),
                "policy_hash": getattr(output, "policy_hash", None),
                "dry_run": bool(getattr(output, "dry_run", False)),
            },
        ))
    return batches


def summarize_batches(batches: List[TrialBatch]) -> Dict[str, Any]:
    return {
        "batches": len(batches),
        "n_trials": sum(b.n_trials for b in batches),
        "families": [b.family_key for b in batches],
    }
