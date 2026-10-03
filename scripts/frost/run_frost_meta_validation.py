#!/usr/bin/env python3
"""
run_frost_meta_validation.py — P8 メタ検証 CLI (`qed frost sensitivity / ablation` 相当)

使い方:
  # 合成データ (golden dataset 抽出前の代用) で Markdown レポートのみ
  python scripts/frost/run_frost_meta_validation.py --synthetic 120 --out reports/frost_meta.md

  # 候補 JSON (FrostCandidate フィールドの配列) を入力に、DB にも保存
  QED_PG_DSN=... python scripts/frost/run_frost_meta_validation.py \
      --candidates-json cands.json --write-db --out reports/frost_meta.md

  --analyses threshold_sensitivity,axis_ablation,weight_perturbation (既定: 全部)

config は環境変数ベース (load_frost_config) + CLI 上書き (--top-k / --promotion-top-k)。
観測のみで、ポリシー・既存テーブルは一切変更しない。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from analytics.python.frost.frost_config import load_frost_config  # noqa: E402
from analytics.python.frost.frost_contracts import FrostCandidate  # noqa: E402
from analytics.python.frost import frost_meta_sensitivity as M  # noqa: E402

_CAND_FIELDS = {f.name for f in dataclasses.fields(FrostCandidate)}


def load_candidates_json(path: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for d in data:
        kw = {k: v for k, v in d.items() if k in _CAND_FIELDS and k not in ("created_at", "updated_at")}
        out.append(FrostCandidate(**kw))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FROST P8 メタ検証")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--candidates-json", help="FrostCandidate dict の JSON 配列")
    src.add_argument("--synthetic", type=int, help="合成候補 N 件 (tests/fixtures/frost_synthetic)")
    ap.add_argument("--seed", type=int, default=20261003)
    ap.add_argument("--runs", type=int, default=M.DEFAULT_PERTURBATION_RUNS)
    ap.add_argument("--noise", type=float, default=M.DEFAULT_WEIGHT_NOISE)
    ap.add_argument("--deltas", default="-0.2,-0.1,0.1,0.2")
    ap.add_argument("--analyses", default=",".join([M.ANALYSIS_THRESHOLD, M.ANALYSIS_ABLATION, M.ANALYSIS_WEIGHT]))
    ap.add_argument("--top-k", type=int)
    ap.add_argument("--promotion-top-k", type=int)
    ap.add_argument("--out", help="Markdown 出力先 (未指定なら標準出力)")
    ap.add_argument("--json-out", help="JSON 出力先")
    ap.add_argument("--write-db", action="store_true", help="frost_meta_validation に保存 (QED_PG_DSN)")
    a = ap.parse_args(argv)

    if a.synthetic:
        from tests.fixtures.frost_synthetic import make_synthetic_candidates
        candidates = make_synthetic_candidates(n=a.synthetic)
    else:
        candidates = load_candidates_json(a.candidates_json)

    config = load_frost_config()
    over = {}
    if a.top_k is not None:
        over["top_k"] = a.top_k
    if a.promotion_top_k is not None:
        over["promotion_top_k"] = a.promotion_top_k
    if over:
        config = dataclasses.replace(config, **over)
        config.validate()

    report = M.run_meta_validation(
        candidates, config,
        deltas=[float(x) for x in a.deltas.split(",") if x.strip()],
        n_runs=a.runs, noise=a.noise, seed=a.seed,
        analyses=[x.strip() for x in a.analyses.split(",") if x.strip()],
    )
    md = M.render_markdown(report)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"[meta] report -> {a.out}", file=sys.stderr)
    else:
        print(md)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")

    if a.write_db:
        dsn = os.environ.get("QED_PG_DSN")
        if not dsn:
            print("ERROR: --write-db には QED_PG_DSN が必要", file=sys.stderr)
            return 2
        import psycopg
        from analytics.python.pg_io.postgres_meta_validation_bridge import (
            insert_meta_validation, make_meta_run_id)
        with psycopg.connect(dsn) as conn:
            n = insert_meta_validation(conn, report)
            conn.commit()
        if n == 0:
            print("[meta] WARNING: frost_meta_validation 未作成 (migration 085 未適用) — DB 保存スキップ", file=sys.stderr)
        else:
            print(f"[meta] {n} rows -> frost_meta_validation ({make_meta_run_id(report)})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
