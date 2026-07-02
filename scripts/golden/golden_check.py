#!/usr/bin/env python3
"""
golden_check.py — QED golden run snapshot / verification (Phase 0)

設計方針:
  - コードベース内部 API に依存しない。パイプライン実行後の
    PostgreSQL テーブル内容だけを正とする(README 記載のテーブル名のみ使用)
  - 揮発列(timestamp / run_id / uuid 等)をパターン除外し、
    残列で正規化 → 浮動小数点を丸め → 全列タプルで正準ソート
  - baseline: スナップショットを tests/golden/baseline.json に保存
  - check:    再スナップショットして baseline と比較。差分があれば exit 1

使い方:
  export GOLDEN_PG_DSN="postgresql://postgres:postgres@localhost:5432/qed_golden"
  python scripts/golden/golden_check.py baseline
  python scripts/golden/golden_check.py check

環境変数:
  GOLDEN_PG_DSN        必須。golden 専用 DB(本番 DSN を渡さないこと)
  GOLDEN_BASELINE_PATH 省略時 tests/golden/baseline.json
  GOLDEN_ROUND_DECIMALS 省略時 10 (Phase 7 の numpy 化以降は 8 を想定)
"""

import json
import os
import re
import sys
from pathlib import Path

import psycopg

# README 記載のテーブルのみ。存在しないものは警告してスキップ
# (v2 テーブル未適用の環境でも動くように)
TABLES = [
    # EML
    "eml_alpha_runs",
    "eml_alpha_candidates",
    "eml_backtest_runs",
    "eml_backtest_folds",
    "eml_alpha_evaluations",
    "eml_alpha_promotion_bridge",
    # FROST v1
    "frost_runs",
    "frost_fitness_candidates",
    "frost_evaluations",
    "frost_selection_decisions",
    "frost_promotion_bridges",
    "frost_audit_event_bridges",
    # FROST v2 / 周辺レイヤー
    "causal_runs",
    "causal_candidate_tests",
    "causal_invariance_results",
    "causal_promotion_gate",
    "alpha_genome_profiles",
    "alpha_genome_clusters",
    "alpha_genome_similarity_edges",
    "frost_crowding_scores",
    "frost_fragility_surfaces",
    # 共通
    "knowledge_artifacts",
    "audit_events",
]

# 実行ごとに必ず変わる列を名前パターンで除外する。
# 注意: candidate_hash / spec_hash 等の「内容ハッシュ」は安定なので除外しない
VOLATILE_PATTERNS = [
    r".*_at$",          # created_at / updated_at / evaluated_at ...
    r"^id$",            # serial / uuid PK
    r".*_id$",          # run_id, candidate_id, backtest_run_id, fold_id ...
    r"^trace_id$",
    r".*uuid.*",
    r".*timestamp.*",
]
_VOLATILE = [re.compile(p) for p in VOLATILE_PATTERNS]

ROUND_DECIMALS = int(os.environ.get("GOLDEN_ROUND_DECIMALS", "10"))


def _is_volatile(col: str) -> bool:
    return any(p.match(col) for p in _VOLATILE)


def _normalize(value):
    """JSON 化可能かつ決定論的な表現へ正規化する。"""
    if isinstance(value, float):
        if value != value:  # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return str(value)
        return round(value, ROUND_DECIMALS)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    # Decimal / date / datetime / その他 → 文字列化
    try:
        f = float(value)
        return round(f, ROUND_DECIMALS)
    except (TypeError, ValueError):
        return str(value)


def snapshot(dsn: str) -> dict:
    result = {}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            existing = {r[0] for r in cur.fetchall()}

        for table in TABLES:
            if table not in existing:
                print(f"  [skip] {table} (テーブル不在)", file=sys.stderr)
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (table,),
                )
                all_cols = [r[0] for r in cur.fetchall()]
                cols = [c for c in all_cols if not _is_volatile(c)]
                if not cols:
                    print(f"  [skip] {table} (安定列なし)", file=sys.stderr)
                    continue
                col_sql = ", ".join(f'"{c}"' for c in cols)
                cur.execute(f'SELECT {col_sql} FROM "{table}"')  # noqa: S608
                rows = [
                    {c: _normalize(v) for c, v in zip(cols, row)}
                    for row in cur.fetchall()
                ]
            # 全列値による正準ソート(主キーの知識を要求しない)
            rows.sort(key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False))
            result[table] = {"columns": cols, "row_count": len(rows), "rows": rows}
            print(f"  [ok]   {table}: {len(rows)} rows", file=sys.stderr)
    return result


def diff_tables(base: dict, cur: dict) -> list[str]:
    problems = []
    for table in sorted(set(base) | set(cur)):
        if table not in cur:
            problems.append(f"{table}: baseline に存在するが今回欠落")
            continue
        if table not in base:
            problems.append(f"{table}: baseline に無い新規テーブル(baseline 再作成要)")
            continue
        b, c = base[table], cur[table]
        if b["columns"] != c["columns"]:
            problems.append(f"{table}: 列構成が変化 {b['columns']} -> {c['columns']}")
            continue
        b_set = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in b["rows"]}
        c_set = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in c["rows"]}
        missing = sorted(b_set - c_set)
        added = sorted(c_set - b_set)
        if missing or added:
            msg = [f"{table}: 行差分 (消失 {len(missing)} / 追加 {len(added)})"]
            for r in missing[:3]:
                msg.append(f"    - {r[:200]}")
            for r in added[:3]:
                msg.append(f"    + {r[:200]}")
            problems.append("\n".join(msg))
    return problems


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("baseline", "check"):
        print(__doc__)
        return 2
    mode = sys.argv[1]

    dsn = os.environ.get("GOLDEN_PG_DSN")
    if not dsn:
        print("ERROR: GOLDEN_PG_DSN 未設定。本番 DSN との取り違え防止のため、"
              "QED_PG_DSN からのフォールバックは意図的に行わない", file=sys.stderr)
        return 2
    if "qed_golden" not in dsn:
        print("WARNING: DSN に 'qed_golden' を含まない DB を対象にしています",
              file=sys.stderr)

    baseline_path = Path(os.environ.get(
        "GOLDEN_BASELINE_PATH", "tests/golden/baseline.json"))

    print(f"[golden] mode={mode} round={ROUND_DECIMALS}", file=sys.stderr)
    snap = snapshot(dsn)

    if mode == "baseline":
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(snap, sort_keys=True, ensure_ascii=False, indent=1))
        print(f"[golden] baseline 保存: {baseline_path}")
        return 0

    if not baseline_path.exists():
        print(f"ERROR: baseline が存在しない: {baseline_path}", file=sys.stderr)
        return 2
    base = json.loads(baseline_path.read_text())
    problems = diff_tables(base, snap)
    if problems:
        print("[golden] FAIL — 差分検出:")
        for p in problems:
            print(p)
        return 1
    print(f"[golden] PASS — 全 {len(snap)} テーブル一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
