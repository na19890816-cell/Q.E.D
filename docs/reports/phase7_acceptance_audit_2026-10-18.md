# Phase 7 — 原本受入・前向き監査接続準備

2026-10-18。**Go は独立 raw JSON 保管契約と合成ソフトウェア試験まで。実原本受入・本番接続・研究採用は Hold**。
実原本 bytes / 正式 Producer manifest / 辞書 / 時刻根拠 / 期待 universe は未取得。
91 snapshot・446銘柄・各履歴の存在は利用者報告であり、実測の件数や探索 N ではない。
EC2・本番 DB・cron・SSH 鍵・売買・crisisEngine への接続／変更は行っていない。

## 基準と依存関係

- main: `a3e63907c2f529f289101698ee7d62f8bf72f436`、変更なし。
- PR #7: `54061f4c4bd3c7b67e46387478a283d7f244c85a` の Exporter 差分を確認。
- 本 Phase は PR #5 `35d670f7181c870cfffd1a5da037e6ad41b66796` を起点とする独立 branch
  `feat/phase7-raw-json-artifact`。新 Draft の base は `feat/phase5r-local-ingestion`。
- 保管 runtime は #5（さらに #4）依存。前向き監査の standalone probe は #6 を含む隔離 checkout が必要。
  #7 は仕様の基準。既存 PR の base・履歴・Draft 状態は変更しない。
- 実装試験対象 commit: `a7490fcbbe0d5721bf8fa25ef0dfde1131e832d8`。
  後続 commit は本報告・生証跡・手動実行済み roundtrip script の追加。
- 使い捨て統合: #2→#1→#3→#4→#5→#6→#7→本 Phase。競合なし。統合 branch は push しない。
  推奨順は既存 #2→#1→#3→#4→#5→#6 と、#5 後に本 Phase。#7 は独立の監査記録。
  将来の #4/#5 merge 後の retarget は人間による差分審査が必要で、今回は実施しない。

## 成果物と実原本の適合

`docs/contracts/phase7_original_sample_request.md` に最小サンプル・承認・秘匿性・読み取り専用手順と
適合表を固定した。snapshot、candidates、JVRAM、OVX/regime、margin manifest は全件
**NOT_RECEIVED / 適合未判定**。single JSON と JSONL をファイル名から推定しない。
匿名化・切り出しは derived。未受領親を hash だけで受領済みにしない。

`docs/contracts/raw_json_artifact_v1.md` と JSON Schema に、新しい
`urn:qed:artifact:raw-json:1` を定義し、専用 receiver を追加した。
既存 record v1 / strict JSONL transport v1 は byte 単位で不変。
raw bytes/空白/改行を不変保存し、SHA/size・strict UTF-8/JSON・depth/node limits・親 artifact・
同一 ID の証拠競合を検証。原本保管の CONTRACT_VALIDATED は研究上の意味論検証ではない。
原本の欠損・不適格・重複銘柄を黙って補正せず、辞書取得前の logical count は検証済みとしない。
regime/candidates を feature v1 に変換していない。

`docs/runbooks/phase7_prospective_and_safety_review.md` に実相互試験の未実施範囲、将来証拠、
3営業日の実資源・CPU credit・日次容量・backup/復元の審査計画を記録した。
未知の過去 N/code/policy/availability を補完していない。

## 今回実行したテスト

同一 Python 3.12.14 / pytest 9.1.1 環境、DB 接続なし。skip を成功件数に含めない。
新規81件は単体50件＋ローカル filesystem 統合31件、全件成功・skip/xfailなし。

| 比較対象 | 成功 | 失敗 | skip |
|---|---:|---:|---:|
| PR #5 HEAD 基準、変更前 | 1876 | 3 | 70 |
| PR #5 + 本実装、変更後 | 1957 | 3 | 70 |
| 既存 v1 / JSONL + 新 artifact の対象試験 | 351 | 0 | 0 |
| PR #1〜#7 統合、追加前 | 2068 | 0 | 156 |
| PR #1〜#7 + 本実装、追加後 | 2149 | 0 | 156 |

両比較とも新規失敗 ID・削除 test ID・追加 skip ID・重複 test ID は0。
PR #5 単独基準の既存3失敗は次の同一 ID。#2 の固定パス修正を含む統合状態では全て成功する。

```
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float_opt
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_json
```

skip は既存の DB/任意依存等によるもので、DB 試験は本 Phase で再実行していない。
正式親スキーマ実結合7件は引き続き Hold（test_pipeline_integration.py の ExperimentReport 2、
KnowledgeArtifact 2、TargetResolution 2、full_pipeline_rerun_no_duplicates 1）。
Phase 6 の PostgreSQL 2217成功/7件deselected は過去記録で、今回の実行結果に加算しない。

## 永続 receipt・障害・前向き監査

通常保管、manifest/raw/receipt 照合、再送、ACK 喪失、同一 ID 別内容/metadata、同日別観測版、
1byte 改変、strict JSON 異常、不完全 manifest、親未着/破損、stream 中断、write barrier/dir fsync
失敗、容量不足、thread/独立 process 同時受信、実 SIGKILL、再起動、ローカル copy 復元を試験。
完了 raw は再検証して復旧、不完全 prefix は隔離、改ざん receipt の RESEARCH_ELIGIBLE は拒否する。

`phase7_artifact_roundtrip.py` を実行し、合成 raw→Producer 完了 manifest→永続 raw/receipt→ACK→
restart→同一再送→1byte 改変隔離を再現した。原本・manifest・intents・receipt・ACK を生証跡に保存。
全て合成であり、実 Producer/Receiver 相互試験10項目の実データ実施件数は **0**。
研究資格は全て false、Producer 原本の削除権限も false。

前向き probe では独立した合成 registry を使用し、監査未指定・未登録・完了 attempt 再利用・
破損 start を callback 前に拒否。予定 run の監査欠落で研究 release を停止し、実 journal の
link publication failure で次の評価が開始されず writer が閉じることを確認した。
registry にも登録されなかった run の全体検出や、実 launcher への enforcement は未実装。
実コードの audit=None default、S1/S2/S3 N_raw、DSR/PBO/FROST/昇格処理は変更していない。
保護対象10ファイル、record schema/validator、JSONL store の hash 不変を照合した。

## 証跡・変更ファイル

- `phase7_validation_2026-10-18.json`: failure/skip ID、比較、版、保護ファイル hash、probe 結果。
- `phase7_evidence_manifest_2026-10-18.json`: 生証跡の全 file SHA/size。
- `evidence/phase7_2026-10-18.tar.gz`: 合成 logs/XML/roundtrip/receipt/ACK、28 files。
  SHA-256: `480651b40951d7938c72905616f147eafaac061338dc52ed58e3c09a737179f0`。
- 追加ソース: `analytics/python/data_contracts/raw_json_artifact.py`。
- 追加契約: sample request、本 artifact 仕様、`qedschema/contracts/raw_json_artifact_v1.schema.json`。
- 追加試験: `tests/fixtures/raw_json_artifact.py`、`tests/unit/test_raw_json_artifact.py`、
  `tests/integration/test_raw_json_artifact_store.py`。
- 追加手順/補助: prospective/safety runbook、validation replay runbook、
  `scripts/audit/phase7_prospective_audit_probe.py`、`phase7_artifact_roundtrip.py`。
  既存 file の内容変更・migration・baseline 再生成はない。

## 保証範囲と未解決事項

local owner-controlled POSIX の flock/link/fsync、atomic terminal receipt、API 上書き禁止まで。
所有者の全再生成・末尾/全消去、実電源断/媒体故障、NFS、外部署名、WORM、off-host backup、
外部 registry/tail 固定を保証しない。root 初期化途中の未分類 staging は安全のため自動採用せず、
人間による隔離・新 root の provision が必要になり得る。inode/容量枯渇時は失敗 receipt 自体も
保存できず、監視・容量修復・recover が必要。永久停止 stream の timeout、全走査の増大、
資源測定、認証 ACK、backup/保持解除も運用接続前の課題。

次の優先順位は、①承認済み最小原本/manifest/辞書/期待集合/時刻根拠の正式取得、
②実相互試験、③独立 run registry と外部固定/backup 復元、④3営業日の資源審査。
**Go: 合成ソフトウェア検証。Hold: 実受入・本番接続・統計採用。**
新 Hard Gate、NOTE-008 採用、親スキーマ7件も Hold。Draft 解除・merge は行わない。
