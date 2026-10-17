# Phase 5R Task A/B 検証記録 — 2026-10-17

main `a3e63907c2f529f289101698ee7d62f8bf72f436` 起点の独立ブランチ
`feat/phase5r-deployment-data-contract`。PR #1〜#3を取り込まず、main・本番DB・実売買・
ProStock2のcronに変更を行っていない。今回の完了範囲はTask A/Bの配置監査と受入契約・純関数である。

## 成果物と変更範囲

- [配置計画](../architecture/qed_deployment_plan.md): Python/PG/外部schema/入口/ファイルと資源、環境の責任分界、PR依存と提案マージ順序。
- [データ契約](../contracts/prostock2_audit_v1.md)と[JSON Schema](../../qedschema/contracts/prostock2_audit_v1.schema.json): version/ID/時刻/版/hash/gate/status/理由/欠損を固定。
- `analytics/python/data_contracts/__init__.py`, `prostock2_v1.py`: 既存FROST DTOを使わない独立したstrict境界。DB・ネットワーク・保存・現在時計を使わない。
- `tests/fixtures/prostock2_contract.py`: 明示的な合成fixture。実市場履歴ではない。
- `tests/unit/test_prostock2_contract.py`, `tests/integration/test_prostock2_contract_pipeline.py`: 単体・Schemaから純関数までの合成統合。
- `requirements-contract-test.txt`: 開発用jsonschema固定版。validator runtimeは標準ライブラリのみ。
- 本報告と同名の機械可読結果JSON。

受入validatorやテスト以外のanalytics、既存DSR/PBO/FROST policy/Hard Gate、探索・昇格ロジック、
既存migration、golden baselineには差分がない。ProStock2用コードや実行環境を既存EC2へ配置していない。

## Task Aの監査結果

ProStock2既存EC2にQ.E.D/PG/EML/FROSTはない、という確定事項で計画を作り直した。
Codex Cloudの既存PGは合成fixtureと外部最小stubを含む機能試験環境で、本番履歴とは扱わない。
依存固定/実データ/親schemaが不足しており、本番に必要なCPU/RAM/永続容量は算定不能。
2vCPU・4GiB・永続20GiBという値は小規模開発用の計画上の出発点で、実データ能力を保証しない。
容量式、未取得のデータ量、別研究worker・別DB・原証拠storage・backupの境界を配置計画へ記載した。

既存入口のsample/synthetic fallback、dry-runでもDBへ書くFROST、DSNを表示する初期化script、
エラーを隠すMakefile経路を識別し、受入から自動実行しない。
コード全体を作り直すことも、それらの動作を今回変更することもしていない。

確認時のPRはすべてmain `a3e6390` 基準・Draft・unmerged:

| PR | HEAD | 役割 |
|---|---|---|
| #1 | fbb0ef26a5a5f889dedf9aeed8d858635da68aa0 | S3・capture・088/089等の台帳監査 |
| #2 | 99604b427716a760a949d85b311f4ecb9728804c | 固定パス解消・外部結合の明示 |
| #3 | 44c974b585f292e91d62de3f7bb8b7b3a3343c11 | NOTE-008の実データ不足・研究Hold |

提案順は **#2 → #1 → #3 → 本PR**。本PR自体は各PRのコードに依存しない。
更新baseで回帰・失敗ID比較を行い、人間が承認してから扱う。マージ・Draft解除は行わない。

## Task Bの保証とテスト

全instantをUTC Z wire形式に限定し、naive・UTC以外のoffset・不正暦日を拒否する。
価格/特徴量/マクロ値は精度を保つdecimal文字列、volumeはstrict safe integerとする。
不明は理由付きnullとし、0、仮hash、推定日時を作らない。payload欠損でもmeasurement_idで対象を保持する。

型・必須/未知キー・JSON重複・NaN/Inf・bool代用・hash・missing_fieldsの完全一致、
OHLCVの範囲/確定bar公開順序、入力受信→特徴量計算→特徴量公開の順序、capture上限を検証する。
非readyでも既知の時刻矛盾やhash不一致を成功扱いにしない。
batchでは同ID重複/異証拠競合、自然キー重複、依存閉包・一致する全証拠hash/時刻・循環、
明示された期待集合に対する日付/銘柄/特徴量の欠落を検証する。

米国マクロでは実公表UTCとIANA location/現地date/clockの一致を検証し、冬/夏の08:30 ETの
13:30Z/12:30Z、誤った固定冬時間、存在しないDST現地時刻をテストした。
日本市場での最初の利用判定は、明示的・版固定のdecision時刻列だけを使い、休日や翌営業日を生成しない。
公表元・受信証拠やそのcalendar自体の真実性は未検証であり、値の申告だけでは保証しない。

| 検証 | 成功 | 失敗 | skip |
|---|---:|---:|---:|
| 新規単体 | 132 | 0 | 0 |
| 合成JSON→Schema→意味論→閉包→decision統合 | 13 | 0 | 0 |
| 新規対象合計 | **145** | **0** | **0** |
| DBなし全体・変更前main | 1606 | 3 | 70 |
| DBなし全体・変更後 | 1751 | 3 | 70 |

全体前後の失敗IDは次の3件で完全一致。全体exit codeは前後とも1で、全件成功と報告しない。
これらの修正は独立Draft PR #2にあり、本PRに混在させていない。

```
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float_opt
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_json
```

70skipはDB接続変数を外した既存モード等による。新規145件にはskip/xfailはない。
外部親schemaに依存する7件は実DBテストを再実行しておらず、引き続き **Hold／実結合未検証**。
新規統合はDBや永続保存を伴わないソフトウェア統合であり、この7件の代わりではない。
中間実行では期待error codeと特徴量fixtureの時刻定義の調整を行い、最終定義で対象と全体を再実行した。

Schema構文・正常小数/小数秒regex、内部リンク、`git diff --check`も確認した。
測定環境はPython3.12.14、pytest9.1.1、jsonschema4.25.1、IANA tzdata **2026b**。
合成例は品質検証専用であり、統計的アルファ、実データの可用性、DSR適格性、実運用採用を証明しない。

```bash
python -m pip install -r requirements-contract-test.txt
python -m pytest tests/unit/test_prostock2_contract.py tests/integration/test_prostock2_contract_pipeline.py -q -o addopts= --tb=short
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN -u GOLDEN_PG_DSN python -m pytest tests/ -q -o addopts= --tb=short -rs
git diff --check
```

再実行時はcheckout rootをPYTHONPATHへ指定する。ログ/XML/比較JSONは
`/workspace/work/qed-phase5r-audit/` に保存した。秘密値・DSN・生の入力をエラー文へ含めない境界を検証し、
実認証情報や実データをfixture/PRへ保存していない。

## 残る取得情報と判断

**Go**: Task A/B提案とソフトウェア検証を独立Draft PRでレビューする。
**Hold**: 正式な親schema・生産者仕様との照合、実DB結合、NOTE-008採用、運用配置。

必要なのはデータ提供責任者/取得先/権限、親DDLの固定版と制約、exportの安定ID/版/上流gate定義、
実公開/初回受信/計算ログ、universe/取引calendar版、原市場データとhash、全探索記録とN_raw、
非生存候補を含む実FROST履歴、コスト後OOS returns、量/頻度/retention/RPO/RTOである。
過去の不明なNを1や0へ補完しない。DSR/PBO/既存policyは維持する。

Task Cのraw-file SHA照合、原子的保存、不完全ファイル拒否、再送の永続冪等性、元証拠の上書き防止、
障害再試行/隔離、取込完了/失敗receiptは **未実装**。今回のpayload/evidence hashと純batch重複検査を、
それらの実装完了と混同しない。Task Dの将来全試行保存の詳細監査・不足対応も後続とする。
