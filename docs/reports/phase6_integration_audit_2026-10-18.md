# Q.E.D Phase 6 — PR 統合回帰と実 Exporter 受入準備

2026-10-18。[固定 HEAD・全変更ファイル](phase6_pr_heads_2026-10-18.json)、
[機械可読検証結果](phase6_integration_validation_2026-10-18.json)、
[再現手順](../runbooks/phase6_integration_replay.md) を合わせて参照する。
本 PR は main `a3e63907c2f529f289101698ee7d62f8bf72f436` 起点の監査・検証 script・設計のみ。
統合した既存70ファイルや local merge commit を本 PR へ混入させない。
既存 PR の Draft/base/履歴・main・本番接続は変更しない。

## 6A: 依存関係と順序

| PR | 固定 HEAD | 追加／変更file | base・依存 |
|---|---|---:|---|
| #1 | fbb0ef2 | 26 | main、既存084へS3/capture/immutability追加 |
| #2 | 99604b4 | 6 | main、固定パス3件修正・親依存7件の分類 |
| #3 | 44c974b | 4 | main、実データ研究Holdの文書 |
| #4 | c92cef1 | 11 | main、配置・record v1契約 |
| #5 | 35d670f | 9 | #4のbranch、Task C |
| #6 | 3bca51d | 14 | main、Task Dのopt-in監査 |

PR の固有差分どうしで共通変更fileは **0**。unionは70file。
#5 の inherited #4 差分を再加算しない。全70fileが統合後も各 owner PR の blob と完全一致した。
使い捨て branch で #2→#1→#3→#4→#5、続いて #6 を merge し、競合はなかった。
新しい replay script でも別 worktree/branch から同順序を実行し、tree hash・テストID・件数を再現した。
元のテストID消失と重複IDはともに0。想定外 import 失敗・新 runtime dependency は発生しなかった。

推奨順は検証した **#2→#1→#3→#4→#5→#6**。
#5 は現状 #4 branch が base なので、将来 #4 を人間が承認・mainへmergeした後、
#5 の base を main へ変更し9file差分を再確認する工程が別途必要。本 Phase では変更していない。
#6 は独立しているが、ここでは最後に組み込んだ状態を検証した。
本結果は実際のmerge/Draft解除や本番 rollout の承認ではない。

## 6A: 回帰結果

同じ venv・Cloud 環境、合成 fixture。baseline を再生成せず既存 suite を実行した。
full DB試験の skip は成功扱いせず、parent 7件の deselection と分ける。

| 範囲 | 成功 | 失敗 | skip | deselected |
|---|---:|---:|---:|---:|
| main・DBなし | 1606 | 3 | 70 | 0 |
| #2→#1→#3→#4→#5・DBなし | 1985 | 0 | 156 | 0 |
| #6追加後・DBなし | 2068 | 0 | 156 | 0 |
| PR #1単独・台帳/capture実PG | 89 | 0 | 0 | 0 |
| 全PR統合・同じ台帳/capture実PG | 89 | 0 | 0 | 0 |
| 全migration二重適用・既存standalone fixture | 1 | 0 | 0 | 0 |
| DB統合初回・FROST view未適用 | 211 | 7 | 0 | 7 |
| repository view適用後・DB統合 | 218 | 0 | 0 | 7 |
| view適用後・全体PG standalone | 2215 | 0 | 2 | 7 |
| 隔離SciPy参照・main | 2 | 0 | 0 | 0 |
| 隔離SciPy参照・全PR統合 | 2 | 0 | 0 | 0 |
| 隔離依存補完後・全体PG standalone最終 | **2217** | **0** | **0** | **7** |
| Cloud中断・再起動後の全体PG再実行 | **2217** | **0** | **0** | **7** |

DBなしの三段階は独立 replay でも同じ件数・ID。new failure IDは0。
main の以下3失敗は PR #2 により統合後に成功へ変化した。

- `TestFrostWriterImport::test_module_imports_safe_float`
- `TestFrostWriterImport::test_module_imports_safe_float_opt`
- `TestFrostWriterImport::test_module_imports_safe_json`

いずれも `tests/unit/test_phase6_base_writer.py` の `/home/user/prostock` 固定パス依存。
DBなしskipの70→156はPR #1の追加PGケース86件による。PGで同じ89 IDが単独/統合とも成功した。
先行全体PGの2skipは `test_phase4_dsr` のSciPy参照。依存を隔離導入した別実行では双方2成功。
さらに隔離依存を指定した全体PGで2217成功/skip0を確認した。先行実行の履歴は改記していない。
追加skip/xfailやテスト削除はない。

初回DB失敗は `test_frost_integration` の test_18〜24。`v_frost_runs` 等の未作成とその後の
transactionエラーに起因した。実repositoryのviews 060〜063を適用して全218件が成功した。
この初期化依存は失敗記録・hash付き適用DDLとしてJSONへ残した。テストのassertは変更していない。

親アプリのevidence_bundles/factor_candidatesを必要とする7 IDはJSONに明示し、未実行/Hold。
既存の全migration fixtureにある外部最小stubはstandaloneの範囲だけで使用した。
台帳89件と追加migration matrixはそのstubを使わず、実Q.E.D DDLだけの別空DBで実行した。

## 6A: 不変条件と追加probe

DSR/PBO/score/gate/decision/promotionとS1/S2/純粋台帳の10 source file、golden baselineは
mainとbyte一致。S3/capture/Task C/Dを含む全owner差分も各PR HEADと一致する。
既存のaudit有無比較は統合状態で成功し、EMLの式・fitness・順序・SearchStats、
FROSTの評価値・score・gate・decision/rank/promotion eligibilityが一致した。
異なる独立runの比較ではUUID・作成時刻だけを正規化し、既存の数値や判定を変えない。

既存S1/S2の同run・異証拠へのDO NOTHING、S1/S2/S3を跨る原証拠の完全照合は未解決のまま。
Dのrun再使用拒否は同audit rootの範囲で、別rootや非監査runの重複は検知しない。
下記の合成fixtureの合計N=7を、過去探索の網羅性や統計的独立性の証明とは扱わない。
未知の過去Nはnull/Holdを維持する。

追加の `phase6_migration_probe.py` は新規専用DBで次を実証した。

- S1の合成SearchStats（generated=5/distinct=3）を変換するとN=3、SR統計は空。
  S2はduplicate hashを1回、EMLを除外してN=2。
  S3完全一覧N=2。合計7、SR十分統計count=2。各経路の同一再送は加算しない。
- 084→086→087→088→089を2回再適用して既存全行値とcapture再取得が不変。
  依存前の086/087/088/089は拒否。084→087は086なしで動く。
- 実FROST writerとS2台帳をaudit無効/有効の別runで動かし、評価・判断・S2行/十分統計が一致。
  各runのS2 N=2、auditによる追加台帳書込は0。dry_runで実昇格0、外部parent stubなし。
- late commit前のcaptureはN=7。commit後の通常live as-ofはN=8へ変化するが、同じcaptureはN=7を保持。
  **遅延commitによるlive再問合せの変化は未解消**。recorded_atはcommit可視時刻ではない。

`phase6_cross_component_probe.py` はCの保存集合がDの記録で変化せず、C再送/隔離もDへ影響しないことを確認。
正常readyもPROVENANCE_INCOMPLETE、ACKに研究/削除許可なし。ローカルbackup/restore後に
raw/manifest/receipt/event hashを検証し、末尾event削除を別local checkpointとの差で検出した。
合成の未監査runも独立期待registryから検出した。外部registryやcheckpointの運用実装ではない。

## 6B/6C: 実原本・運用の残件

[Exporter差分とraw JSON artifact設計案](../contracts/prostock2_exporter_acceptance_gap.md) に項目別条件を保存。
指定された実原本は添付・正式提供されておらず、tracked repositoryにも該当原本はない。
したがってraw SHA、manifest/receipt照合、market_date根拠、観測版、446銘柄と期待集合の差は未検証。
pretty JSONを隔離して元bytesを保持する合成試験は成功したが、実snapshotの形式を示さない。
single JSONは別artifact契約で原本保存し、変換物を元hashへリンクする提案とした。未実装。
regime/candidateの専用契約判断は正式辞書待ちで、既存feature/v1の意味は変えない。

[backup/復旧設計](../runbooks/qed_backup_recovery_design.md) は転送/永続ACK、未送原本の保持、
off-host copy、外部tail固定、run registry、容量/inode/研究停止・漏洩防止を整理した。
実off-host backup、外部署名/WORM、媒体故障/停電/DB dump復旧、本番RPO/RTOは未検証。

local FS上4KiB×64fileの合成測定では、file fsync p50=0.483ms/p95=0.776ms、
directory fsync p50=0.166ms/p95=0.249ms、logical同期組=約1207/秒。
観測時空き約27.31GB、inode約196.7万。これは共有検証FSの瞬間値。
物理IOPS、本番容量・保持期間・CPU/RAM・throughputは未算定/Holdで、採用基準へ使用しない。

## 変更ファイルと判定

- `scripts/audit/run_phase6_regression.py` — pinned HEADの安全なlocal統合・全体replay
- `scripts/audit/phase6_migration_probe.py` — 空DB migration/台帳/snapshot/実writer比較
- `scripts/audit/phase6_cross_component_probe.py` — C/D独立性・合成local復元/fsync
- `docs/reports/phase6_pr_heads_2026-10-18.json`
- `docs/reports/phase6_integration_validation_2026-10-18.json`
- 本監査記録
- `docs/runbooks/phase6_integration_replay.md`
- `docs/contracts/prostock2_exporter_acceptance_gap.md`
- `docs/runbooks/qed_backup_recovery_design.md`
- `docs/reports/evidence/phase6_2026-10-18.tar.gz` — 合成試験の生log/JUnitとprobe結果
- `docs/reports/phase6_evidence_manifest_2026-10-18.json` — bundleと各entryのSHA/size

## 実行環境中断後の回収

最終試験確認後にCloud execution transportが切断され、先行commitではGitHub connectorで
記録6fileを保存した。環境復帰後に元の3script・生log/XML・probe/replay結果を再取得し、
SHAを再計算して本PRへ追加した。先行commitの「未回収」は当時の状態で、今回解消した。
再起動後のC source/backup/restoreの3集合は中断前local checkpointと一致し、receipt/eventも一致。
PG containerの中断時exit=255（OOM=false）から再起動し、固定capture16件の再取得hashを確認した。
同じtest DBにある意図的破損fixture1件はchecksum拒否された。これは既存負例testがINSERTした
payload={}・zero hashで、未知の再起動破損として扱わない。他のreplay失敗は0。
全体PG再実行も2217成功/失敗0/skip0、親7件deselected、process exit=0を再確認した。
これは実媒体喪失・off-host・外部anchorの保証ではない。終了確認はJSON/bundleに記録する。

**Go**: 指定順でのソフトウェア統合回帰、人間によるPR統合レビュー、正式原本受入準備の設計。
**Hold**: 親schema実結合7件、実Exporter/原本検証、off-host/外部固定/実機復旧、本番rollout、
研究資格付与、全探索N確定、cost後returns、NOTE-008採用評価、新Hard Gate。
**Reject**: 不明な時刻・N・OHLCVの推測補完、無断JSONL化した派生物を原本と呼ぶ運用、
合成fixtureを統計的edgeや実運用採用の証拠にする判定。
main merge、既存PR base/履歴変更、Draft解除、本番接続は行わない。
