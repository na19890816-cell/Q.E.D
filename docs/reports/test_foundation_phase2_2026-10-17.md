# Phase 2 — テスト基盤の整備（2026-10-17）

main `a3e63907c2f529f289101698ee7d62f8bf72f436` 起点の独立ブランチ。
Phase 1のPR #1、migration086〜089、capture、S3実装は含めない。
本番DB書込み、baseline再生成、DSR/PBO/既存ゲート/昇格計算の変更はない。

## A: 固定パス3件 — 完了

`test_phase6_base_writer.py` の `/home/user/prostock` 固定パスを廃止した。
テスト自身の `__file__` からcheckout rootを解決し、writer/base_writerの実ロード元を照合する。
3関数のaliasが実際のbase_writer関数と同一オブジェクトか検証し、文字列部分一致による偽陽性も防ぐ。
FROST packageを通常初期化してからwriterをimportし、単体初回import時の循環を避ける。

対象ファイルは変更前75成功/3失敗、変更後 **78成功・失敗0・skip0**。
別cwdでclass5件、独立プロセスでimport3件が成功。
誤alias・他checkout由来のmodule cacheを意図的に渡す負例2件も拒否を確認した。
実装側のimportやFROSTコードは変更していない。

## B: 外部7件 — 依存整理を実施、実結合は未検証 / Hold

Q.E.D内には `evidence_bundles` / `factor_candidates` の実DDLがなく、
利用列だけからFK/CHECK/INDEX/DEFAULTを推測して作成しなかった。
docsが参照する親名 `prostock-bff-v2`、README旧作業名由来 `prostock` を同ownerでGitHub照会したが、現資格で双方HTTP404。
`/user` とQ.E.D取得は成功しており、403や全体認証失敗ではない。不存在と非公開アクセス不足は区別不能。
可視workspaceにも正式な親schemaは見つからず、正確な親repo URL・固定commit・権限、または公式schema-only dumpが必要。

既存7件に `parent_app_integration` markerを付けた。通常実行から除外せず、skip/xfailを追加していない。
active factor_candidateのfixtureがない場合の既存skipは明示的な失敗へ変更した。
実schema・正式fixtureを取得していないので、7件が成功したとは報告しない。

新しい `inspect_parent_schema.py` はUTF8、REPEATABLE READ / READ ONLYでcatalogを観測する。
列型・NULL・DEFAULT・制約・index・triggerをJSONとSHAに記録し、DSNや生の接続例外は出力しない。
テーブルの存在だけで実結合成功とはせず、常に `integration_verified=false` を出力する。
ローカル専用DBでは `evidence_bundles` / `factor_candidates` / `hypotheses` 欠落を検出し、期待どおり終了コード2。
SQL_ASCII指定DSNをUTF8へ正規化した観測も終了コード2で動作した。
設定欠落・不正DSNは終了コード1、秘密値をエラーへ含めないことも確認した。

公式初期化・fixture構築の実施は未完了。
[初期化手順と検証範囲](../runbooks/test_database_setup.md) に必要資料と実行順を記載した。
既存migrationテストのstubを拡張して7件を通す方法は採用していない。

## 同一環境での全体回帰

Python 3.12.14 / PostgreSQL 17.11 / psycopg 3.3.6 / pytest 9.1.1。
変更前はmain専用のclean worktree、変更後はPhase 2 worktreeで実行し、PYTHONPATHを各checkout rootに固定した。
ローカル専用 `qed_migration` を利用。既存最小stubを持つため、このDBは親アプリ実環境とはみなさない。

| 検証 | main a3e6390 | 修正後 |
|---|---|---|
| DBなし `tests/` | 1606成功・3失敗・70skip（exit1） | **1609成功・失敗0・70skip（exit0）** |
| 実PostgreSQL `tests/integration/` | 61成功・7失敗・skip0（exit1） | **61成功・7失敗・skip0（exit1）** |

PostgreSQL失敗IDは前後で完全一致。新規失敗・skip増加なし。
Phase 1の1712等とはブランチ内容が異なるため、件数の大小で回帰を判定しない。
marker collectionでは **7件選択 / 61件deselected** で、指定された7件だけに一致した。
ここでのdeselectedはcollection確認であり、未実行を成功件数へ加えていない。
DBなしskipは実DB環境変数を外したモードの既存設定によるもので、成功扱いしない。
新CLIはcompileと実DB read-only smokeを確認。`git diff --check` を通過した。

実DBで残る失敗:

```
tests.integration.test_pipeline_integration.TestPhaseBExperimentReport::test_bridge_creates_experiment_run
tests.integration.test_pipeline_integration.TestPhaseBExperimentReport::test_bridge_idempotent
tests.integration.test_pipeline_integration.TestPhaseCKnowledgeArtifact::test_ka_bridge_creates_artifact
tests.integration.test_pipeline_integration.TestPhaseCKnowledgeArtifact::test_ka_bridge_idempotent
tests.integration.test_pipeline_integration.TestPhaseDTargetResolution::test_resolved_when_valid_candidate_code
tests.integration.test_pipeline_integration.TestPhaseDTargetResolution::test_unresolved_when_no_code
tests.integration.test_pipeline_integration.TestUpsertNoDuplicates::test_full_pipeline_rerun_no_duplicates
```

再現コマンドはrunbookに記載。ログ・JUnit・比較JSONは `/workspace/work/qed-phase2-audit/`、
GitHub親repo照会のHTTP記録は `/workspace/work/qed-pr1-review/phase2-parent-*.http` に保存した。

## 変更ファイル・判断

- `tests/unit/test_phase6_base_writer.py`
- `tests/integration/test_pipeline_integration.py`
- `pytest.ini`
- `scripts/postgres/inspect_parent_schema.py`
- `docs/runbooks/test_database_setup.md`
- 本報告

**Go**: Aの修正、検証範囲の分類、読取専用の依存観測。**Hold**: Bの実結合保証。
親schema固定版と推移的依存先・正式fixtureが得られるまで、本物のfixture/初期化は構築できない。
統計実験・候補モデル/重み/閾値探索を実施していないため、p値・効果量・信頼区間は対象外。
次はPhase 3の実FROST履歴・全探索N・観測時刻・returnsの所在を確認し、未取得なら採用評価を停止する。
