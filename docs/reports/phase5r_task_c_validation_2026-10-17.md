# Phase 5R Task C — 契約照合・原本受入の検証記録

2026-10-17。比較基準は PR #4 HEAD `c92cef13ed01542145c2e97989efce88f9bd64e5`。
branch `feat/phase5r-local-ingestion`、PR base は `feat/phase5r-deployment-data-contract`。
main 起点の Task D と分離する。PR #4 の既存差分を本 PR の追加変更として扱わない。

## 実施内容と状態

[C-0 の項目別照合](phase5r_c0_contract_alignment_2026-10-17.md) を先に実施した。
利用者提示の 91 snapshot／446 銘柄は直接検証済みの実データではない。
OHLCV 原本、過去 code/policy、availability の証拠が不足し、一括 v1 変換は承認できない。
数値 regime 指標は条件付き feature 候補、カテゴリ regime と candidate 履歴は別契約が必要。
旧原本は transport archive として保持し、v1 の意味／enum は変更しない。

[受入仕様](../runbooks/local_audit_ingestion.md) に従い、DB なしのローカル受信、raw-byte SHA、
bounded strict JSONL、no-overwrite publication、fsync、transfer/record の競合検知、部分重複の再利用、
隔離、immutable intent/receipt、再起動復旧、ACK を実装した。
manifest の追加許可フィールドや、checksum を計算し直した不正な研究適格状態も拒否する。
正常 ready も `PROVENANCE_INCOMPLETE` で停止し、研究資格付与 API は実装していない。

依存検査は閉じた batch の record evidence/time と、同 store へ正常受信済みの原市場 raw hash
の実在照合を区別する。原市場原本の未受信／隔離は `SOURCE_ARTIFACTS_UNVERIFIED` として保持する。
hash が一致しても真実性・全期間網羅・当時の利用可能性を証明しない。
現時点で正式な ProStock2 原本・Exporter・承認は未提供であり、実ファイルの照合は未実施。

## テスト結果

Python 3.12.14、同じ venv・Cloud 環境、DB 接続設定なし、すべて合成 fixture。
既存 baseline は再生成していない。新規の skip／xfail は追加していない。
集計・failure ID・JUnit hash は [機械可読記録](phase5r_task_c_validation_2026-10-17.json) を参照。

| 実行範囲 | 成功 | 失敗 | skip | exit |
|---|---:|---:|---:|---:|
| 新規単体 | 92 | 0 | 0 | 0 |
| 新規ローカル FS 統合 | 33 | 0 | 0 | 0 |
| 新規 + PR #4 対象 | 270 | 0 | 0 | 0 |
| 全体・変更前 PR #4 | 1751 | 3 | 70 | 1 |
| 全体・変更後 | 1876 | 3 | 70 | 1 |

失敗 ID と skip ID は前後で完全一致。新規失敗なし。
既存 3 失敗は `tests/unit/test_phase6_base_writer.py` の固定パス `/home/user/prostock` に起因する。
PR #2 の独立修正を混入して成功扱いせず、以下を失敗のまま記録した。

- `TestFrostWriterImport::test_module_imports_safe_float`
- `TestFrostWriterImport::test_module_imports_safe_float_opt`
- `TestFrostWriterImport::test_module_imports_safe_json`

既存 skip 70 件は検証成功を意味しない。親スキーマ未取得の外部依存 7 件は引き続き Hold。
PostgreSQL、本番 DB、EC2、cron、取引システムには接続していない。

対象実行は以下（repository root、同 venv）。

```text
PYTHONPATH=$PWD /workspace/Q.E.D/.venv/bin/python -m pytest -q \
  tests/unit/test_local_ingestion.py \
  tests/unit/test_ingestion_manifest_profile.py \
  tests/integration/test_local_ingestion_recovery.py \
  tests/unit/test_prostock2_contract.py \
  tests/integration/test_prostock2_contract_pipeline.py
PYTHONPATH=$PWD /workspace/Q.E.D/.venv/bin/python -m pytest -q
```

正常・同一再送・同一 ID 別証拠・部分重複・不完全 manifest・途中破損・日時／銘柄欠落・
不正 availability・旧原本・不正昇格を検証した。FS 統合では source 障害、ENOSPC、write/fsync
中断、durable receipt 後の応答喪失、実 SIGKILL の partial/complete 境界、thread/process 同時受信、
raw/manifest/receipt/intent 改変を検証した。容量不足を skip で逃げず fault injection で再現した。
独立レビューで見つかった既存 link 再利用時の fsync 不足と intent 型検査不足を修正し、回帰試験を追加した。

## 変更ファイル

- `analytics/python/data_contracts/local_ingestion.py`
- `qedschema/contracts/ingestion_manifest_v1.schema.json`
- `docs/runbooks/local_audit_ingestion.md`
- `docs/reports/phase5r_c0_contract_alignment_2026-10-17.md`
- `tests/unit/test_local_ingestion.py`
- `tests/unit/test_ingestion_manifest_profile.py`
- `tests/integration/test_local_ingestion_recovery.py`
- 本記録と対応する JSON

PR #4 の v1 validator/schema、既存 FROST・DSR・PBO・Hard Gate・昇格、golden baseline に差分なし。
追加したコードから既存の研究／売買経路への自動接続なし。

## 永続保存の保証と未完了事項

既存・永続済み parent 下の owner-managed local POSIX FS で、file/directory fsync と exclusive link
が正しく実装されることが前提。成功返却後に raw・manifest・receipt が再取得でき、保存済み bytes を
上書きしない。fsync 失敗で可視 link が残った場合も、再利用／再読込時に耐久性 barrier をやり直す。
不完全 prefix は原本と区別し、未検証の record_count は null。受信時刻は実際の現在時刻であり、
過去 available_at や producer captured_at として使用しない。

未対応は NFS／複数 host、媒体故障・虚偽 fsync・実機停電、全体の意図的改ざん／削除、外部署名／WORM、
off-host backup、無期限 block の内部 timeout、byte-offset 転送再開、長期 GC／大規模索引である。
容量不足で失敗 receipt すら書けない場合は例外・intent 残存として fail-closed にし、容量回復後に復旧する。
manifest 引数は decoded mapping であり、不正 manifest の元 transport bytes は API へ渡されていない。
その原本まで必要なら別 artifact 提供が必要。不正値・source 例外本文を receipt やログへ出さない。

既定上限は raw 16 MiB／行 256 KiB／10,000 行、store 1 GiB、reserve 16 MiB。
一時 raw と最終 raw の同時存在、metadata、inode、backup 分を別に確保する。
単一受信 worker で履歴再検証 I/O が増える方式であり、本番 throughput／RAM／保持容量の計測は未実施。
必要条件は Python と tzdb、永続 POSIX disk、独立 worker・資源制限。PostgreSQL は不要。

Exporter 残差は stable ID／完了 manifest／期待集合の根拠、過去 capture と availability、
当時の code/policy/universe/calendar/vintage、原市場 bytes、regime/candidate 新契約、
認証した ACK の保存照合と削除承認である。保存期限だけで未転送原本を削除する処理は実装しない。

**Go**: 合成 fixture による独立ローカル受信のソフトウェア品質。
**Hold**: 実 Exporter 相互接続、正式原本検証、研究資格付与、NOTE-008 採用、親 DB 実結合 7 件。
p 値・効果量・CI・CPCV・コスト後 OOS・DSR によるアルファ検証は実施していない。
main／Draft 状態／本番接続は変更しない。
