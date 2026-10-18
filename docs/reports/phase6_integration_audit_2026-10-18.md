# Q.E.D Phase 6 — PR統合監査
2026-10-18。main基準: a3e63907c2f529f289101698ee7d62f8bf72f436。

[固定HEADと全70変更ファイル](phase6_pr_heads_2026-10-18.json)、
[機械可読結果](phase6_integration_validation_2026-10-18.json)、
[再現手順](../runbooks/phase6_integration_replay.md)を参照する。
このPRはmain起点の文書・検証記録のみ。既存PRの統合コードやlocal merge commitを混入させない。

## 保存時の制約
下記試験を実行・結果確認した後、Cloud execution transportが切断され、環境はoffline/startingになった。
GitHub connectorは利用できたため、確認済み結果と設計をこのPRへ保存した。
Cloud内の未コミット再現/probeスクリプト、生JUnit/XML、完全ログは再読込・回収・アップロードできていない。
最終PG XMLのSHAは切断前に計算していないのでnullとする。post-restartのworkspace整合性も未検証。
この記録は生ログの代替ではない。独立した最終承認は原証跡回収・再照合までHold。
テスト成功を取り消す理由は観測していないが、再取得できていない証拠を保存済みと扱わない。
専用PGコンテナ/volumeのcleanupも未確認。原本や既存環境を削除する操作は行っていない。

## 6A: HEAD・依存関係・順序
| PR | HEAD | 固有変更file | base |
|---|---|---:|---|
| #1 | fbb0ef2 | 26 | main |
| #2 | 99604b4 | 6 | main |
| #3 | 44c974b | 4 | main |
| #4 | c92cef1 | 11 | main |
| #5 | 35d670f | 9 | PR #4 branch |
| #6 | 3bca51d | 14 | main |

共通変更fileは0、unionは70。#5のinherited #4差分は再計上しない。
新しい使い捨てworktree/branchで #2→#1→#3→#4→#5、その後#6を組み込み、競合なし。
全70fileが各owner PRの固定HEADと一致した。別worktreeでの完全replayも同件数・同test IDを再現した。
元test ID消失0、重複ID0、新import失敗0。新runtime dependencyは追加していない。

推奨順序は検証した #2→#1→#3→#4→#5→#6。
将来#4が人間承認でmainへmergeされた後、#5をmain baseへ変更し9file差分を再確認する工程が必要。
今回base変更、rebase、force-push、merge、Draft解除は行っていない。

## 6A: テスト結果
すべて合成fixture。同じCloud/venvでbaselineを比較し、既存baselineは再生成していない。

| 実行 | 成功 | 失敗 | skip | deselected |
|---|---:|---:|---:|---:|
| main DBなし | 1606 | 3 | 70 | 0 |
| PR #1〜#5 DBなし | 1985 | 0 | 156 | 0 |
| PR #1〜#6 DBなし | 2068 | 0 | 156 | 0 |
| PR #1単独 台帳/capture実PG | 89 | 0 | 0 | 0 |
| 全PR統合 同じ台帳/capture実PG | 89 | 0 | 0 | 0 |
| 全migration二重適用 standalone | 1 | 0 | 0 | 0 |
| DB統合初回 FROST view未適用 | 211 | 7 | 0 | 7 |
| 実repository view適用後 DB統合 | 218 | 0 | 0 | 7 |
| 全体PG SciPy未導入 | 2215 | 0 | 2 | 7 |
| 隔離SciPy参照 main | 2 | 0 | 0 | 0 |
| 隔離SciPy参照 統合 | 2 | 0 | 0 | 0 |
| 隔離依存を指定した全体PG最終 | **2217** | **0** | **0** | **7** |

mainの既存3失敗はtest_phase6_base_writer.pyの固定パスによるもので、PR #2で全て成功へ変化した。
対象はTestFrostWriterImportのtest_module_imports_safe_float、safe_float_opt、safe_json。
新規失敗IDは0。DBなしskip増加86件はPR #1の追加PGケース。
台帳PG89 IDはPR #1単独と統合で一致して全成功。

DB初回7失敗はtest_frost_integrationのtest_18〜24で、v_frost_runs等の未作成と続くtransactionエラー。
repositoryのviews 060〜063を適用して解消した。assertやskip指定は変更していない。
SciPy参照2件は1.16.3を別directoryへ--no-depsで導入して確認し、最終全体PGもskip0となった。
先行失敗/skipの実行履歴は上書きしない。

正式な親schema/fixtureを必要とする7件は収集のみ、実行せずdeselected/Hold。
existing全migration fixtureの最小外部stubはstandaloneに限定し、親実結合の成功としない。
台帳89件と追加migration probeでは外部parent stubを使っていない。

## 6A: 不変条件
DSR/PBO/ScoreEngine/GateEngine/decision/promotion、純粋台帳、S1/S2変換の10 source fileとgolden baselineはmainとbyte一致。
統合状態で既存audit有無比較が成功し、EMLの式/fitness/順序/SearchStats、
FROSTの評価/score/gate/decision/rank/promotion eligibilityが一致した。
別runのUUID・作成時刻だけを正規化し、数値や判定を変えない。

新規空DBのprobeでは、合成SearchStats(generated=5,distinct=3)をS1 N=3へ変換、
S2のduplicate hashは1回、EMLを除外してN=2、S3完全一覧N=2、合計N=7/SR count=2を確認。
同一再送は加算しない。実FROST writerをaudit無効/有効の別runで動かし、
評価/判断/S2行と十分統計が一致し、各run S2 N=2、auditからの追加台帳書込0、実昇格0。
pipelineはdry_run=True、実Q.E.D DDLのみを使用した。

084→086→087→088→089の二回再適用で全行/captureが不変。
依存前の086/087/088/089は拒否、084→087は086なしで成立。
late commit前captureはN=7、commit後live as-ofはN=8へ変化し、同captureはN=7のまま。
live再問合せの変化は未解消。recorded_atはcommit可視時刻ではない。

既存S1/S2の同run異証拠DO NOTHINGと、S1/S2/S3を跨る完全原証拠照合は未解決。
Dのrun再使用拒否は同audit root内だけ。未知の過去Nはnull/Holdを維持する。
合成N=7は過去探索の網羅性や統計的独立性の証明ではない。

Cの保存集合はDの記録で変化せず、C再送/隔離もDに影響しない。
readyでもPROVENANCE_INCOMPLETE、ACKに研究/削除許可なし。
pretty single JSONの元bytes隔離、local backup/restore、別local checkpointによるtail削除検出、
独立期待registryからの合成未監査run検出も成功した。外部運用の実装ではない。

## 6B/6Cと残件
[実Exporter差分とraw JSON案](../contracts/prostock2_exporter_acceptance_gap.md)、
[backup/復旧設計](../runbooks/qed_backup_recovery_design.md)を保存した。
実原本/manifest/辞書は未受領で、raw SHA、market_date、観測版、446銘柄と期待集合の差は未検証。
regime/candidatesをfeatureへ無理に変換せず、既存v1は変更しない。

4KiB×64fileのlocal合成測定はfile fsync p50=0.483/p95=0.776ms、
directory fsync p50=0.166/p95=0.249ms、logical同期組約1207/秒。
空き約27.31GB/inode約196.7万は共有検証FSの瞬間値。
物理IOPS、実データ容量、必要CPU/RAM、保持期間、本番RPO/RTOは未算定。
off-host、外部署名/WORM、実機停電/媒体喪失、DB dump復元は未検証。

## 成果物と判断
本PR変更はこの記録、対応JSON、固定HEAD JSON、再現手順、Exporter差分、backup設計の6file。
local未コミットの3検証scriptと生ログは回収待ちで本PRに含まない。

Go: 観測したソフトウェア統合回帰、正式原本受入準備の設計。
Hold: 生証跡/スクリプト回収と独立最終承認、親実結合7件、実Exporter/原本、
off-host/外部固定/実機復旧、本番rollout、研究資格、全探索N、cost後returns、
NOTE-008採用、新Hard Gate。
Reject: 時刻/N/OHLCVの推測補完、無断変換した派生物を原本と呼ぶ運用、
合成結果による統計的edge/実運用採用。
