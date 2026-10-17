# ADR-002 S3 検証記録 — 2026-10-17

ベースは最新 `origin/main` の `a3e63907c2f529f289101698ee7d62f8bf72f436`。
Python 3.12.14、PostgreSQL 17.11、pytest 9.1.1、psycopg 3.3.6 で実行。
DB はオンボーディング時のローカルテスト専用 DB（最小外部テーブルスタブ + 全 migration + FROST views）。
本番 Q.E.D. 本体スキーマは含まれない。

## 結果

| 検証 | 変更前 main | S3 変更後 |
|---|---|---|
| DB 接続変数なし `tests/` | 1606 passed / 3 failed / 70 skipped | 1646 passed / 3 failed / 83 skipped |
| 実 PostgreSQL `tests/integration/` | 60 passed / 7 failed / 1 skipped | 74 passed / 7 failed / 0 skipped |
| S3 単体 | 未実装 | 39 passed |
| S3 実 PostgreSQL | 未実装 | 13 passed |

変更後の実 DB 全体実行では `QED_MIGRATION_TEST_DSN` も設定し、全 migration の二重適用テストを実行した。
DB なしの追加成功 40 件は S3 単体 39 件と、SQL ファイルを列挙する既存 migration 検査の 086 分 1 件。
追加スキップ 13 件は新規 S3 の実 DB テスト。既存スキップには SciPy が未導入の参照値比較 2 件も含まれる。
変更前後の失敗 node ID 集合を比較し、DB なし 3 件・実 DB 7 件とも完全一致した。新規失敗はない。
全体スイートの終了コードは両方 **1**。全テスト成功とは扱わない。

## 既存失敗（今回の修正対象外）

`tests/unit/test_phase6_base_writer.py::TestFrostWriterImport` の次の 3 件は、
`/home/user/prostock/analytics/python/io/postgres_frost_writer.py` の絶対パスが固定されており、
`/workspace/Q.E.D` では FileNotFoundError になる。

- `test_module_imports_safe_float`
- `test_module_imports_safe_float_opt`
- `test_module_imports_safe_json`

`tests/integration/test_pipeline_integration.py` の次の 7 件は外部スキーマ不足。
`test_resolved_when_valid_candidate_code` は `factor_candidates`、残りは `evidence_bundles` が存在しない。

- `TestPhaseBExperimentReport::test_bridge_creates_experiment_run`
- `TestPhaseBExperimentReport::test_bridge_idempotent`
- `TestPhaseCKnowledgeArtifact::test_ka_bridge_creates_artifact`
- `TestPhaseCKnowledgeArtifact::test_ka_bridge_idempotent`
- `TestPhaseDTargetResolution::test_unresolved_when_no_code`
- `TestPhaseDTargetResolution::test_resolved_when_valid_candidate_code`
- `TestUpsertNoDuplicates::test_full_pipeline_rerun_no_duplicates`

## 新機能の検証

完全性の宣言、N の過少/過大申告、型・時刻の不正、重複 JSON キー、証拠ハッシュ、
同じ式の正規化、同じ証拠のラベル・順序・整形変更、CLI 検証/登録/再送を確認した。
実 DB で UNIQUE 制約、同時登録（同一 ID / 別 ID）、差し替え拒否、部分書き込みのロールバック、
呼び出し元トランザクションのロールバック、append-only、未来完了時刻拒否、as-of を確認した。
追加テストで発見した「呼び出し元の最初の操作時に内部トランザクションが早期 commit する」問題は修正し、再実行で成功した。

既存 DSR / FROST / 昇格判定のソースは変更していない。新規 SR 統計は登録しない。
S3 が保証するのは提出された証拠との整合性、一意性、DB 登録時刻であり、提出元が隠した試行や
虚偽の出典・時刻を SHA-256 だけで証明することはできない。元の完全ログとの監査が必要。

## 再実行

リポジトリルートで仮想環境を有効化し、DB なし確認では接続変数を外す。

```bash
python -m pytest tests/ -q -o addopts= --tb=short -rs

# テスト専用 DB の DSN を環境設定から指定
# QED_PG_DSN と QED_MIGRATION_TEST_DSN を設定して実行
python -m pytest tests/integration/ -q -o addopts= --tb=short -rs

python -m pytest tests/unit/test_adr002_s3_manual_ledger.py
python -m pytest tests/integration/test_adr002_s3_manual_pg.py
```

全体テストのログと JUnit は現在の環境の `/workspace/qed-cloud/s3/` に保存。
開発ブランチへの commit / push と Draft PR 作成は別の操作として確認する。main へのマージは行わない。
