"""
eml_lineage.py
--------------
ADR-002 S1: EML 探索結果 → 試行台帳 (TrialBatch) への変換 (純関数)。

- 1 回の探索呼び出し (SearchStats) = 1 TrialBatch
- n_trials = SearchStats.n_trials = 評価した異なる式の数 (top_k 切り捨て前)
    * 同一式の再評価は同一の検定なので 1 試行 (EML exhaustive は大量の木が同一式に縮退する)
    * exhaustive と gradient が同じ式を出した場合は両方に計上する (過少計上しない側)
- sr_stats は空のまま記録する:
    * 探索時 fitness は rank IC であり Sharpe ではない
    * 評価段階の Sharpe は top_k 生存者のみ = 選択済み標本で分散が過小 → V[SR] に使うと
      SR0 が下がり DSR が楽観化するため不採用 (ADR-002 §4.2)
  → DSR は SR 推定量分散へフォールバックする (帰無下の SR 標本分散 = 保守側)
- fitness 統計・生存者 Sharpe は metadata に参考値として保存する
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from analytics.python.frost.frost_lineage import (
    TrialBatch,
    family_spec_dict,
    make_family_key,
)

#: EML の calc_sharpe は年率 (sqrt(252)) 換算
_ANNUALIZATION = 252


def eml_family_key(output: Any) -> str:
    """EMLDiscoveryOutput から family_key を作る。"""
    return make_family_key(
        horizon=output.target_horizon,
        universe=output.universe,
        target=output.target_name,
        terminal_set_hash=output.terminal_set_hash,
    )


def _survivor_sharpes(output: Any, stage: str) -> List[float]:
    """評価済み生存者の非年率 Sharpe (参考値)。"""
    modes = {c.candidate_id: (c.metadata or {}).get("search_mode") for c in output.candidates}
    out: List[float] = []
    for ev in output.eval_results or []:
        if modes.get(getattr(ev, "candidate_id", None)) != stage:
            continue
        try:
            v = float(getattr(ev, "sharpe", float("nan")))
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v / math.sqrt(_ANNUALIZATION))
    return out


def trial_batches_from_eml_output(
    output: Any,
    recorded_at: Optional[datetime] = None,
    source_type: str = "eml",
) -> List[TrialBatch]:
    """
    EMLDiscoveryOutput.search_stats を TrialBatch のリストへ変換する。

    batch_id は (run_id, stage, seq) から決定論的に生成されるため、
    同じ run を再記録しても台帳は重複しない。
    """
    fam = eml_family_key(output)
    spec = family_spec_dict(output.target_horizon, output.universe,
                            output.target_name, output.terminal_set_hash)
    batches: List[TrialBatch] = []
    seq_by_stage: Dict[str, int] = {}
    for st in output.search_stats or []:
        seq = seq_by_stage.get(st.stage, 0)
        seq_by_stage[st.stage] = seq + 1
        surv = _survivor_sharpes(output, st.stage)
        batches.append(TrialBatch.create(
            family_key=fam,
            run_id=output.run_id,
            stage=st.stage,
            n_trials=st.n_trials,
            sharpes=None,  # 意図的に空 (モジュール docstring 参照)
            seq=seq,
            trace_id=output.trace_id,
            source_type=source_type,
            family_spec=spec,
            recorded_at=recorded_at,
            metadata={
                "search_stats": st.to_dict(),
                "batch_label": output.batch_label,
                "survivor_sharpe_daily": {
                    "count": len(surv),
                    "values": [round(v, 10) for v in surv],
                    "note": "top_k 生存者のみ (選択バイアスあり)。V[SR] には不使用",
                },
            },
        ))
    return batches
