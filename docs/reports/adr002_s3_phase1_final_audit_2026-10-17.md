# Phase 1 — ADR-002 S3 最終監査（2026-10-17）

実装品質について **マージ推奨**。PR #1 は Draft のまま、人間の承認を待つ。
main `a3e63907c2f529f289101698ee7d62f8bf72f436` へのPush・マージ、FROST昇格、本番DB書込みは行っていない。
研究上の優位性や実運用採用を認定する報告ではない。

比較対象は PR #1 の `e08ce4d9fc559e063282caed2d461b7fc4fd62f8`。
Python 3.12.14 / PostgreSQL 17.11 / psycopg 3.3.6 / pytest 9.1.1 の同一環境で変更前後を測定した。
既存baselineは再生成せず、既存DSR・PBO・FROST・ハードゲート・昇格条件・純台帳コアの計算に差分はない。
既存DB読取bridgeも今回のPhase 1では変更していない。

## 見つかった問題と修正

1. **部分重複による再計上**。従来の一覧hashでは `{A,B}` の後に `{A}` / `{A,B,C}` / `{A,C}` を別IDで登録できた。
   一覧v2 hashは維持し、family・正規化式・UTC評価時刻・UTCデータ締切の評価行hash v1を追加する。
   089の主キーで一行でも重複すれば、batch・receipt・評価行を全てrollbackする。
   完了時刻・ID・式の空白・等価タイムゾーンの変更や、同時部分重複も拒否する。
   Nを自動減算して成功扱いしない。評価時刻が異なる独立再評価の加算は維持する。
2. **TRUNCATEによる消去**。084のbatch/edgeと086 receiptはUPDATE/DELETEのみ拒否していた。
   新規088でBEFORE TRUNCATE文トリガを追加する。087のcapture保護は維持し、089も全3操作を拒否する。
   適用済み084を書き換えず、既存DBへ追加migrationを適用できる。
3. **保存payloadの検証不足**。正しいSHA付きの `version=true` 等を受理し、返却JSONが元のSHA対象と異なる問題を再現した。
   version・整数・有限SR値・一覧・件数・UTC・派生値を検証し、再構成payloadをcanonical JSONで比較する。
   負のSR平均・整数表現のSR値・ゼロ試行batch等、既存コアの合法値は維持する。
4. **SQL_ASCII再送の偽競合**。receiptのTEXTがbytesで返る場合の比較を既存の文字列復号関数で正規化する。

修正前の追加テストは、評価行重複・旧v2・SQL_ASCIIの7件が失敗。
append-only監査は10失敗/8成功（うち7ケースでTRUNCATE成功）。
capture異常payloadはunit29失敗/32成功、実DB10失敗。
captureのred確認は独立プロセスへ `e08ce4d` の該当moduleを読み込んで行い、作業ツリーやbaselineは巻き戻していない。

## 不変条件の照合

| 条件 | 実装・実DB検証と保証範囲 |
|---|---|
| 完全一覧由来N | `N = \|{normalize_formula(row.formula)}\|`。証拠内N・CLI N・検算値の不一致を拒否。敗者・未保存候補も必要。 |
| 同一評価の重複防止 | 一覧hashと評価行hashのDB一意制約。別ID・別完了時刻・部分再梱包・同時登録を拒否。 |
| 独立再評価N_raw | batch内はdistinct式数、独立した評価時刻の別runは加算。既存のstage/run横断加算を変更しない。 |
| append-only | batch・edge・receipt・capture・評価行でUPDATE/DELETE/TRUNCATEを拒否。管理者によるDDL/trigger無効化は認可設計の別問題。 |
| 原子性 | savepoint・FK・一意制約。評価行INSERT障害、部分重複、競合、caller rollbackで部分登録を残さない。 |
| family・系譜 | 既存make_family_keyを使用。48条件の純台帳コアとの照合、祖先有無、循環、将来edge/batchを再検証。 |
| UTC・MVCC | aware入力をUTC化。既存readerの単一SQL、naive UTC互換、非UTC接続、caller所有transactionを再検証。 |
| SR混入禁止 | 証拠JSONにSR入力を許さず、S3 DB書込みのsr_count/mean/m2は0。選抜後SRを分散へ代入しない。 |
| 旧形式混在禁止 | 旧一覧hash、および評価行registry導入前v2のfamilyは原証拠監査まで登録・再送を停止。自動backfillなし。 |
| 原証拠の完全性 | hashは提出バイト・一覧・行の同一性を結び付ける。隠された試行、虚偽の出典・時刻、経済的リークは証明しない。 |

評価行hashは `SHA256(canonical_json(version=1, family_key, trial))`。
原証拠ファイルはDBに複製しないため、完全な上流ログとアクセス制御された証拠保管が必要。
同じ式の独立評価が同じ時刻解像度で記録された場合もhashだけでは識別できない。時刻を作り変えず監査する。
S1/S2から同じ評価をS3へ再梱包してはならず、stage横断の行同一性検知は実装していない。

## 固定保存とmigration

保存後のN・family・batch ID・SR十分統計量・派生値・payload_sha256の一致、同条件の冪等保存、
同時初回保存、遅延commit/rollback、保存失敗後の再実行、CLI replayを全て実DBで再検証した。
SR分散の既存式 `M2/(count-1)` と、payload全体のcanonical JSONに対するSHA-256を検証し、計算式は変更していない。
capture参照は新規API本体と独立CLIだけで、DSR/FROST/昇格判定経路へ自動接続されていない。

**通常のas-of再問合せは履歴不変ではない。** `recorded_at < as_of < commit` ではN=0→2が依然起こる。
REPEATABLE READも新transactionでの再現を保証しない。recorded_at/captured_atはコミット可視性時刻ではない。
captureは最初に観測した入力を保存する方式であり、保存前の任意日時のコミット済み集合を復元しない。

適用順序は084 → 086 → 088 → 089。captureは084 → 087で独立導入でき、086には依存しない。
全機能を使うDBでは084 → 086 → 087 → 088 → 089を適用する。
087旧Draftを試用したDBは更新087も再適用する。新S3 clientには088/089が必要で、旧clientとの混在はサポートしない。
未監査の旧v2 familyを再開する移行ツールは今回提供しない。元証拠の復元・重複監査・移行設計が別途必要。

空の専用DBを作成し、外部親アプリのstubを使わず、実際の5 migrationを適用した。
依存前の086/087/088/089が失敗すること、084→087が086なしで動作することを確認した。
batch・receipt・評価行・captureを保存後、5 migrationをさらに2回適用し、全行値・capture replay・完全一致再送の冪等性を確認。
5テーブルのBEFORE TRUNCATE文トリガも検証した。専用DBは終了後削除し、本番DBは使用していない。
追加の088テストは全DDL・危険操作をrollback専用transaction内で実行する。

## 回帰結果

| 検証 | 変更前e08ce4d | 最終修正後 |
|---|---|---|
| 指定された対象208件 | 208成功・失敗0・skip0 | 全208 IDが引き続き成功 |
| 追加分を含む台帳・S3・capture対象 | 既存208件 | **299成功・失敗0・error0・skip0** |
| DBなし `tests/` | 1659成功・3失敗・116skip | **1712成功・3失敗・156skip** |
| PostgreSQL `tests/integration/` | 107成功・7失敗・skip0 | **147成功・7失敗・skip0** |

対象追加91件はunit51件・実DB40件。全体にはmigrationファイル列挙による既存staticテスト2件も増えた。
DBなしのskip増加40件は実DB対象を実行しないモードの既存設定によるもので、成功件数へ算入していない。
新規テストをskip/xfailへ変更して回避したものはなく、実DBモードではskip0。
対象の警告1件は既存pytest class fixtureの非推奨警告。

前後のJUnit失敗IDは完全一致し、新規失敗はない。全体の終了コードは前後とも1で、全件成功とは扱わない。
既存3件は `/home/user/prostock` 固定パス、既存7件は外部 `evidence_bundles` / `factor_candidates` 不足。
Phase 2へ分離し、PR #1では修正しない。

```
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float_opt
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_json
tests.integration.test_pipeline_integration.TestPhaseBExperimentReport::test_bridge_creates_experiment_run
tests.integration.test_pipeline_integration.TestPhaseBExperimentReport::test_bridge_idempotent
tests.integration.test_pipeline_integration.TestPhaseCKnowledgeArtifact::test_ka_bridge_creates_artifact
tests.integration.test_pipeline_integration.TestPhaseCKnowledgeArtifact::test_ka_bridge_idempotent
tests.integration.test_pipeline_integration.TestPhaseDTargetResolution::test_resolved_when_valid_candidate_code
tests.integration.test_pipeline_integration.TestPhaseDTargetResolution::test_unresolved_when_no_code
tests.integration.test_pipeline_integration.TestUpsertNoDuplicates::test_full_pipeline_rerun_no_duplicates
```

全migration適用の既存テストには外部アプリの最小stubがあるため、その成功は親アプリとの実結合を証明しない。
ここでの保証はQ.E.D所有の台帳schema・API。親アプリの実schema確認とfixture整備はPhase 2で行う。
中間実行では別スイートのDDLを同一専用DBで並行させ6件のdeadlockが発生した。
最終対象・全体実行はDDLスイートを逐次化して全て再確認した。製品の同時登録テストは独立した接続で実行・成功している。

```bash
# QED_PG_DSN: ローカル専用qed_lineage。DSN認証値は記録しない。
.venv/bin/python -m pytest tests/unit/test_adr002_lineage.py tests/integration/test_adr002_lineage_pg.py tests/unit/test_trial_snapshot_capture.py tests/integration/test_trial_snapshot_capture_pg.py tests/unit/test_adr002_s3_manual_ledger.py tests/integration/test_adr002_s3_manual_pg.py tests/integration/test_adr002_ledger_immutability_pg.py -q -o addopts= --tb=short
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN .venv/bin/python -m pytest tests/ -q -o addopts= --tb=short -rs
# 両DB変数をローカル専用qed_migrationに設定。外部stubありのため実結合とは区別。
.venv/bin/python -m pytest tests/integration/ -q -o addopts= --tb=short -rs
git diff --check
```

生ログ・JUnit・失敗ID比較・空DB監査JSONは `/workspace/work/qed-pr1-review/phase1-*`、
088単独のred/green記録は同ディレクトリの `final-audit-immutability-*` に保存した。

## 変更ファイルと判断

- `analytics/python/frost/manual_trial_ledger.py`
- `analytics/python/pg_io/postgres_manual_trial_bridge.py`
- `analytics/python/pg_io/postgres_trial_snapshot_capture.py`
- `scripts/postgres/register_manual_trials.py`
- `qedschema/migrations/088_qed_trial_ledger_no_truncate.sql`
- `qedschema/migrations/089_qed_manual_trial_evaluations.sql`
- `tests/unit/test_adr002_s3_manual_ledger.py`
- `tests/unit/test_trial_snapshot_capture.py`
- `tests/integration/test_adr002_s3_manual_pg.py`
- `tests/integration/test_trial_snapshot_capture_pg.py`
- `tests/integration/test_adr002_ledger_immutability_pg.py`
- `docs/adr/ADR-002-lineage-trial-ledger.md`
- `docs/runbooks/manual_trial_ledger.md`
- `docs/runbooks/trial_snapshot_capture.md`
- 本報告

**Go**: Phase 1実装のレビュー・人間承認へ進む。**Hold**: マージ実行、本番migration、未監査旧familyの再開、実運用採用。
合成fixtureは実装検証専用で、統計的エッジの採用根拠としていない。候補モデル・重み・閾値の探索は実施していないため、
統計検定・p値・効果量・信頼区間・CPCVの結果は本Phaseにはない。
次の優先順位はmain起点の独立ブランチでPhase 2を整備し、実データ/Nの確認後にPhase 3、結果を踏まえてPhase 4とする。
