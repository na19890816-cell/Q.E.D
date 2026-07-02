#!/usr/bin/env python3
"""
golden_dataset.py — golden 用固定データセットの抽出 / ロード (Phase 0)

extract: 本番 DB の event_study_summaries から、キー列の先頭 N 値分の
         行だけを CSV へ書き出す(再現可能な最小データセット)
load:    golden DB の同テーブルを TRUNCATE して CSV を COPY で投入

使い方:
  # 1) 本番から抽出(読み取りのみ。書き込みは一切しない)
  QED_PG_DSN="postgresql://...:5432/qed_dev" \\
  python scripts/golden/golden_dataset.py extract \\
      --key-column symbol --keys-limit 30 \\
      --out tests/golden/dataset/event_study_summaries.csv

  # 2) golden DB へロード(migrations 適用済みであること)
  GOLDEN_PG_DSN="postgresql://...:5432/qed_golden" \\
  python scripts/golden/golden_dataset.py load \\
      --csv tests/golden/dataset/event_study_summaries.csv

前提(未確認・要実機確認):
  - 銘柄を表す列名。READMEに記載がないため --key-column で指定する。
    実際の列名(symbol / ticker / code 等)は \\d event_study_summaries で確認
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg

TABLE = "event_study_summaries"


def extract(args) -> int:
    dsn = os.environ.get("QED_PG_DSN")
    if not dsn:
        print("ERROR: QED_PG_DSN 未設定(抽出元=本番)", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # キー列の存在確認(タイポを実行前に検出)
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            (TABLE, args.key_column),
        )
        if cur.fetchone() is None:
            print(f"ERROR: 列 {args.key_column} は {TABLE} に存在しない。"
                  f"\\d {TABLE} で実列名を確認のこと", file=sys.stderr)
            return 2
        copy_sql = (
            f'COPY (SELECT * FROM "{TABLE}" WHERE "{args.key_column}" IN '
            f'(SELECT DISTINCT "{args.key_column}" FROM "{TABLE}" '
            f'ORDER BY "{args.key_column}" LIMIT {int(args.keys_limit)}) '
            f'ORDER BY "{args.key_column}") '
            "TO STDOUT WITH (FORMAT csv, HEADER true)"
        )
        with out.open("wb") as f, cur.copy(copy_sql) as copy:
            for data in copy:
                f.write(data)
    print(f"[dataset] 抽出完了: {out} ({out.stat().st_size} bytes)")
    print("[dataset] この CSV を git 管理に追加すること(再現性の根)")
    return 0


def load(args) -> int:
    dsn = os.environ.get("GOLDEN_PG_DSN")
    if not dsn:
        print("ERROR: GOLDEN_PG_DSN 未設定", file=sys.stderr)
        return 2
    if "qed_golden" not in dsn:
        # 本番 TRUNCATE 事故の防止。golden 以外への load は明示フラグ必須
        if not args.force:
            print("ERROR: DSN に 'qed_golden' を含まない。本番への TRUNCATE を"
                  "防ぐため中止(意図的なら --force)", file=sys.stderr)
            return 2
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: {csv_path} が無い", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f'TRUNCATE "{TABLE}" CASCADE')
        with csv_path.open("rb") as f, cur.copy(
            f'COPY "{TABLE}" FROM STDIN WITH (FORMAT csv, HEADER true)'
        ) as copy:
            while data := f.read(65536):
                copy.write(data)
        conn.commit()
        cur.execute(f'SELECT count(*) FROM "{TABLE}"')
        n = cur.fetchone()[0]
    print(f"[dataset] ロード完了: {TABLE} = {n} rows")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract")
    pe.add_argument("--key-column", required=True)
    pe.add_argument("--keys-limit", type=int, default=30)
    pe.add_argument("--out", default="tests/golden/dataset/event_study_summaries.csv")

    pl = sub.add_parser("load")
    pl.add_argument("--csv", default="tests/golden/dataset/event_study_summaries.csv")
    pl.add_argument("--force", action="store_true")

    args = p.parse_args()
    return extract(args) if args.cmd == "extract" else load(args)


if __name__ == "__main__":
    sys.exit(main())
