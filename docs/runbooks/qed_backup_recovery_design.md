# Phase 6C — ACK・backup・復旧設計
2026-10-18。実EC2/本番DB/cron/売買への接続なし。
local POSIXのソフトウェア保証と、未実装の運用機能を区別する。

## ACKと保持
| ACK/判断 | 意味 | 削除/研究への効力 |
|---|---|---|
| 転送ACK | sender/transportがbytesを渡した | 許可しない |
| durable ACK | raw/manifest/receiptの再読hash/sizeとfsync完了確認 | コピー保存だけ。削除/研究資格は許可しない |
| 保持解除承認 | 認証ACK、backup、保持policyを照合した別判断 | 将来の人間承認手順、未実装 |

現C ACKはraw SHA/size、manifest SHA、receipt ID/SHA、実確認UTC時刻を束縛する。
research_eligible=false、producer_deletion_authorized=falseを維持する。
ACK自体は認証プロトコル/外部署名ではない。Export前に認証チャネル、ACK保存/再照合、
応答喪失後の完全同一再送を合意する。未転送/未確認/隔離原本を期限だけで自動削除しない。

## backupと外部固定の案
Cはblobs/manifests/intents/receipts/quarantine/診断pending、相対名/size/SHA/参照閉包を保存する。
prefixと完全原本は別扱い。Dは全sequence event、run claim、staging、contextとtail sequence/hashを保存する。
未完了を完了へ補正せず、独立した予定/開始run registryと契約/code/migration版も固定する。
DB backupがあればdump/version/checksumと対象範囲を含める。

backupとは別の管理主体/障害領域でmanifest hash、C receipt hash、D tail hash、
run registry hashを固定する提案とする。鍵分離/失効/時刻/権限/保持/監査を別途設計する。
外部署名、WORM、timestamp service、外部checkpointは未実装。
hash chain単独で全末尾削除やownerによる全再生成を防げるとは保証しない。

raw本文、任意の例外値、認証情報をcheckpoint/PRへ載せない。
実データhashも承認した監査領域で扱う。今回GitHubに保存した数値/hashは合成検証とソフトウェア情報のみ。

## backup手順案
1. producerから独立したreceiver/audit workerをquiesceする。ProStock2のcron/売買を変更せず未送原本を保持。
2. C recover/receipts、D inspectで整合性を確認する。破損/未完了/未知Nは研究Hold。
3. 固定したimmutable集合を別障害領域へコピーし、コピー先の全hash/参照閉包/fsyncを検証する。
   live copyを一貫したsnapshotと呼ばない。
4. 認証/暗号化/鍵管理/アクセス監査/保持/復元試験を満たしたものだけをoff-host backupとする。
5. 外部manifest固定と復元成功を記録してworkerを再開する。原本削除は別承認。

backup先、鍵管理、保持期間、RPO/RTOは未確定。S3等へ今回アップロードしていない。
ローカル別directoryへのcopy/restore試験はoff-host保管の検証ではない。

## 停電・容量不足・媒体喪失からの復元案
1. 書込を止め、旧媒体を隔離する。producer原本を削除しない。
2. 新しい専用媒体/directory/空DBを用意する。
3. 信頼した外部checkpointとmanifestを取得し、path traversal/symlink/所有権/mode/容量/inodeを検査する。
4. raw/manifest/receipt、event chain/context/sequenceと外部tail、run registryを照合する。
   valid prefixだけで完全復旧としない。
5. C未完了intentは完成bytesがsize/hash一致する場合だけ再検証し、prefixは隔離して完全再送。
   Dは未完了を保持。計算resumeは未実装で、新run/searchが前attemptを参照する。
6. DBは正式schema/dumpを空DBへ復元し、migration順序と固定captureの再取得hashを確認する。
   live as-of再計算を旧captureの代用にしない。
7. 不一致/欠落tail/未知Nは研究Hold。推測補修しない。

実機停電、媒体喪失、off-host復元、PG dump復旧は未検証。
今回の合成試験はquiescent local copy→別directory restore→receipt/event/hash照合、
最後のevent削除を別local checkpointとの差で検出する範囲まで。
最終試験後には実際にCloud execution transportが切断された。
その後のworkspace保存物の再取得/完全性確認、テストcontainer/volumeのcleanupは未完了である。
この事故を停電耐性/復旧成功の証明とは扱わない。

## 未監査runとfail-closed
独立registryへ計算前の予定/開始runを登録し、監査必須runとD RUN_STARTED/RUN_FINISHEDを照合する案。
監査なし/開始のみ/未完了trial/tail不一致/収集不能は研究Hold。
registryにも載らないrunは検知できない。既存audit=Noneは維持し、自動有効化しない。
合成の実行済み未監査runを期待registryから検出したが、本番の独立registry運用は未実装。

Cはwrite/fsync/容量不足/不整合で成功ACKを返さない。Dは監査障害後の評価を開始しない。
disk-fullでは失敗receiptも書けないためintent保持と外側の監視/研究停止が必要。
worker timeout/kill/recover、資源上限、アラート配送は未接続。既存Hard Gateは変更しない。

## 資源と測定
C容量はretained raw + 最大受信/一時publicationコピー + metadata + quarantine prefix +
backup/復旧余裕 + reserve。hardlink本体とcopy backupの占有を分ける。
既定16MiB/file、256KiB/line、10000行、store1GiB、reserve16MiBは初期限界で、本番必要量ではない。

Dはevent数E、平均bytes B、block Kに対して概ね E*ceil(B/K)*K + directory/backup、inode約E。
optimizer probe Q=2*steps*EML_node_count*n_initにstart/finishの2Q event等が加わる。QをN_rawへ加算しない。

4KiB×64fileのlocal観測:
- file fsync p50 0.483ms/p95 0.776ms
- directory fsync p50 0.166ms/p95 0.249ms
- logical同期組 約1207/秒
- 観測時空き約27.31GB、inode約196.7万

共有仮想FSの瞬間値であり、物理IOPSは未計測。
実データthroughput、本番p95/p99、必要CPU/RAM/保持容量/RPO/RTOは未算定。
永続disk、独立worker、inode/容量reserve、backup鍵管理と実配置負荷試験までrolloutはHold。

## 漏洩防止
receipt/CLIは固定検査code/hash/件数に限定し、payload/source path/任意例外/DSNを出さない。
Dのcredential-like key拒否は補助で、任意文字列中の秘密を完全検出する機能ではない。
rawはowner専用権限、最小アクセス、承認済み暗号化backupとしPRへ持ち出さない。
誤送時の隔離/公開停止/credential rotationは別手順。今回は実原本/秘密値を公開していない。
