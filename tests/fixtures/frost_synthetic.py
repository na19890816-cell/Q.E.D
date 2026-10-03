"""P8 メタ検証用の決定論的合成候補セット (golden dataset 抽出前の代用)。"""
from __future__ import annotations

import random
import uuid
from typing import List

from analytics.python.frost.frost_contracts import FrostCandidate


def make_synthetic_candidates(n: int = 120, seed: int = 7, n_families: int = 20) -> List[FrostCandidate]:
    r = random.Random(seed)
    out = []
    for i in range(n):
        s = r.uniform(-0.5, 2.5)
        ic = r.uniform(-0.01, 0.10)
        out.append(FrostCandidate(
            candidate_id=str(uuid.UUID(int=i + 1)),
            run_id="meta", trace_id="meta",
            complexity_score=round(r.uniform(0.0, 0.8), 4),
            candidate_hash=f"{i:016x}",
            formula_text=f"expr_{i}",
            source_candidate_id=f"{r.randrange(n_families):08d}-src",
            backtest_summary={"oos_sharpe": s, "max_drawdown": r.uniform(0.02, 0.30),
                              "turnover": r.uniform(0.5, 5.0)},
            metrics={"rank_ic": ic, "ic": ic * 0.8},
            regime_breakdown={k: {"sharpe": s * m + r.gauss(0, 0.3)}
                              for k, m in (("bull", 1.2), ("bear", 0.8), ("crisis", 0.5))},
            fold_results=[{"sharpe": s + r.gauss(0, 0.5), "rank_ic": ic + r.gauss(0, 0.02)}
                          for _ in range(8)],
        ))
    return out
