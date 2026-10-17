# ADR-002 固定スナップショット保存の検証 — 2026-10-17

変更前は PR #1 の `336045c`。main は `a3e6390` のまま変更・マージしない。
Python 3.12.14 / PostgreSQL 17.11 / pytest 9.1.1 / psycopg 3.3.6、ローカルのテスト専用 DB で検証。

## 変更と保証範囲

前回の [追加レビュー](adr002_s3_pr1_review_2026-10-17.md) では、登録後・コミット前の as-of が
後から変わることを確認した。今回は、そのとき実際に読んだ入力を保存して再現する独立 API / CLI を追加した。
保存対象は batch ID 全件・N・SR 十分統計量・family 一覧・頻度・除外件数・snapshot_hash。
payload 全体の SHA-256 も保存・検証する。migration 087 で append-only の保存テーブルを追加する。

family × UTC as-of × SR 頻度 × 祖先設定を保存条件とし、同じ条件なら最初の保存結果を再利用する。
REPEATABLE READ の同じ可視性で全問合せを実行し、同時初回保存の競合は主キー制約と再試行で処理する。
API は idle autocommit 接続に限定して自分のトランザクションを所有し、呼出し元のトランザクションを変更しない。
既存の DSR・FROST・昇格判定・台帳読取り API と手動登録コードは変更していない。

**解消した範囲:** 保存済みの結果は、別の書込みのコミット後・別接続・別トランザクションでも保存 ID から再現できる。
**残る制約:** 任意の過去日時のコミット済み集合を壁時計だけで復元する機能ではない。保存前の観測は復元できず、
既存の live 読取 API の as-of 結果は従来どおり変わり得る。既存の判定経路へ自動適用する変更も行っていない。

## 実 PostgreSQL の確認

- W で手動試行 N=2 を登録して外側を未コミットのまま保持。R で `recorded_at < as_of` の時刻に保存すると N=0。
  W をコミットすると live 読取りは N=2 に変わるが、同じ保存条件・保存 ID は N=0 のまま、全出力が一致する。
  新しい as-of で保存すれば N=2。W の rollback の場合は、後の保存も N=0。
- 初回保存 2 件を、双方が未保存を観測したところで同期。競合後も保存行は 1 件、ID・N・batch ID・統計・ハッシュは一致。
- 最初の問合せと台帳集計の間で W をコミット。保存値は一貫した以前の MVCC 可視性を維持し、live 読取りとの差を確認。
- 祖先有無、SR 頻度の除外、UTC と +09:00 の同値、UPDATE / DELETE 拒否、payload 破損拒否、
  未知 ID と migration 不足の拒否、未来 as-of 拒否、例外時 rollback と再実行、呼出し元トランザクション保護を確認。
- CLI の capture / show と再度の capture が、後から台帳が変わっても同じ保存値を返すことを確認。

新規単体 **11 件**、新規 PostgreSQL **16 件** が成功。既存 S3 の 62 件と合わせた対象テストは **89 passed**。

## 全体回帰

| 検証 | 変更前 `336045c` | 変更後 |
|---|---|---|
| DB 接続変数なし `tests/` | 1647 passed / 3 failed / 92 skipped | 1659 passed / 3 failed / 108 skipped |
| 実 PostgreSQL `tests/integration/` | 83 passed / 7 failed / 0 skipped | 99 passed / 7 failed / 0 skipped |
| 固定保存の新規単体 | 未実装 | 11 passed |
| 固定保存の新規実 DB | 未実装 | 16 passed |

JUnit から失敗 ID を抽出し、DB なし 3 件・実 DB 7 件の集合が前後で完全一致することを確認した。
新規失敗はない。DB なしの成功増加 12 件は新規単体 11 件と migration ファイル検査の 087 分 1 件。
スキップ増加 16 件は新規 PostgreSQL テスト。実 DB は追加した 16 件がすべて成功した。
全体スイートの終了コードは前後とも **1** であり、全件成功とは扱わない。
既存 3 件は `/home/user/prostock` 固定パス、既存 7 件は外部 `evidence_bundles` / `factor_candidates` 不足。
詳細な node ID は [前回の検証記録](adr002_s3_validation_2026-10-17.md) に記載済み。
読取分離レベルの復元確認は環境の既定値に対して比較するようにし、そのテスト 1 件も再実行して成功した。
`git diff --check` 通過。既存判定コード・live 読取・S3 登録コード・既存 migration が未変更であることを差分で確認した。

実行設定とログ:

```bash
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN .venv/bin/python -m pytest tests/ -q -o addopts= --tb=short -rs
# QED_PG_DSN と QED_MIGRATION_TEST_DSN をテスト専用 qed_migration に指定
.venv/bin/python -m pytest tests/integration/ -q -o addopts= --tb=short -rs
# QED_PG_DSN をテスト専用 qed_lineage に指定
.venv/bin/python -m pytest tests/unit/test_trial_snapshot_capture.py tests/integration/test_trial_snapshot_capture_pg.py tests/unit/test_adr002_s3_manual_ledger.py tests/integration/test_adr002_s3_manual_pg.py -q -o addopts= --tb=short
```

JUnit・ログ・JSON 比較を `/workspace/work/qed-pr1-review/capture-*` に保存した。
運用と API の契約は [runbook](../runbooks/trial_snapshot_capture.md) に記載。
