# Phase 6C — 保存 ACK・バックアップ・復旧の設計

2026-10-18。EC2／本番 DB／cron／実売買には接続していない。
実装済みの local POSIX 保証と、未実装の運用機能を区別する。
単一 Cloud 環境での合成コピー・復元は、オフホスト保管や実機停電の検証ではない。

## ACK と原本保持

| 種別 | 意味 | 削除／研究利用への効力 |
|---|---|---|
| 転送 ACK | sender/transport が bytes を渡した。receiver の揮発 buffer だけの場合もある | いずれも許可しない |
| durable ACK | receiver が raw・manifest・receipt を再読し、hash/size と file/directory fsync を確認 | コピー保存の確認。削除・研究資格は許可しない |
| 保持解除の承認 | 認証した durable ACK、backup、保持 policy、法務／監査要件を照合した別判断 | 将来の人間承認手順。今回未実装 |

現 `LocalAuditStore.acknowledge` は raw SHA、size、manifest SHA、receipt ID/SHA と実確認 UTC 時刻を
返す。`research_eligible=false`、`producer_deletion_authorized=false` のまま。
ACK 自体は認証プロトコルや外部署名ではない。正式 Exporter 連携前に送受信主体・認証チャネル・
ACK 保存と照合・応答喪失時の同一原本再送を合意する。
未転送・未確認・隔離・不完全原本は保持期限だけで自動削除しない。cleanup cron は追加しない。

## 保存単位と外部固定の提案

以下をひとまとまりの immutable backup manifest に固定する。

- C: raw blobs、transport manifests、intents、terminal receipts、quarantine、必要な診断用 pending。
  各相対名・byte 長・SHA、receipt ID/SHA と raw/manifest の対応。prefix と完全原本は別扱い。
- D: 全 sequence event、run claim、未完了 staging、run/search/family/policy/code、最終 sequence/hash。
  INCOMPLETE を COMPLETED に補正せず、開始済み・未完了 ID を保持する。
- 独立した予定／開始 run registry と、監査必須か否か、その版と hash。
- 契約／code／migration の固定版、DB dump がある場合は dump/version/checksum と対象範囲。

raw 本文、任意の例外値、秘密値を checkpoint や PR に含めない。実データ hash も秘匿性を
確認した承認済みの監査領域へ置く。今回の GitHub 記録は合成 fixture とソフトウェア情報のみ。

backup と別の管理主体／保管先で、manifest hash、C receipt hash、D sequence/tail hash、run registry
hash を固定する案とする。署名鍵の分離、時刻、追記／削除権限、失効、監査閲覧権限を設計してから
採用する。既存 hash chain は末尾全削除や owner の完全再生成を単独では防げない。
外部署名・WORM・timestamp service・外部 checkpoint は **未実装**。

## バックアップ手順案

1. ProStock2 から独立した receiver/audit worker を停止または quiesce し、書込を止める。
   producer の cron／売買へ副作用を与えず、未送原本の保持を継続する。
2. C の recover/receipts と D の inspect を実行する。破損や未完了を隠さず研究利用を Hold にする。
   D の未完了探索へ推測した終了 count を書かない。
3. 完了済みファイル集合・相対パス・hash を固定し、元の immutable bytes を別の障害領域へコピーする。
   live copy を一貫した snapshot と呼ばない。増分なら固定 manifest が列挙した集合を厳密に保存する。
4. コピー先の全 hash と参照閉包を検証し、ファイル／directory の保存完了と外部 manifest 固定を確認する。
5. 認証・暗号化・鍵管理・アクセス監査・保持期限・復元試験を満たしたコピーだけを off-host backup と扱う。
   バックアップ先・鍵管理・RPO/RTO・保持期間は未確定。S3等へ今回アップロードしていない。
6. 復元試験の成功を記録した後に worker を再開する。元原本の削除可否は別承認。

## 停電・容量不足・媒体故障からの復元案

1. 書込先を隔離し、上書きや自動 retry storm を止める。producer 原本を削除しない。
2. 元媒体に依存しない新しいディスク／専用 directory／空のテスト DB を準備する。
3. 信頼した外部 checkpoint と backup manifest を取得し、相対パス、traversal、symlink、所有権、
   mode、容量／inode を確認して復元する。原本を整形・再符号化しない。
4. C の raw/manifest/receipt hash と dependency closure、D の sequence/context/hash chain と
   外部 tail hash、run registry を照合する。valid prefix だけで完全復旧としない。
5. C の未完了 intent は recover する。完成 bytes が確認できる場合だけ再検証、prefix は隔離し完全再送を求める。
   D の未完了は inspect して保全する。計算再開は未実装で、新 run/search に親 attempt を参照する。
6. PostgreSQL は正式 schema/dump と fixed capture を空 DB へ復元し、migration 順序と再取得 hash を検証する。
   不明な live as-of を過去 capture の代わりに再計算しない。recorded_at と commit visibility は別。
7. 不一致・失われた末尾・未知 N があれば復旧／研究利用を Hold。推測補修で採用を進めない。

実機電源断・媒体喪失・off-host 災害復旧・PostgreSQL dump restore は今回未検証。
本 Phase の probe はローカルの quiescent コピー→別 directory 復元→hash/receipt/event 再検証、
最後の event 削除を独立したローカル checkpoint で検出する合成試験まで。
バックアップと checkpoint が同時に失われる障害への保証はない。

## 未監査 run の検出と fail-closed

予定／開始 run を独立 registry に計算前に登録し、監査必須 run と D の RUN_STARTED/RUN_FINISHED を
照合する案とする。監査が指定されない run、開始のみ、missing trial、tail 不一致、収集不能は研究 Hold。
registry 自体に記録されなかった run は検出できない。既存の `audit=None` は引き続き有効で、
default を自動変更していないため、本番の全 run 監査保証はまだない。
本 Phase では監査なしで実行した合成 run が独立した期待 registry との差分として検出できることだけを試験した。

C の write/fsync/容量不足/不整合は成功 ACK を返さない。D の監査障害は以後の評価を開始しない。
媒体が満杯なら失敗 receipt も書けないため、intent 保持・固定 error code・外側の監視と研究停止が必要。
監視・独立 worker の timeout/kill/recover・資源上限・アラート配送は設計事項で今回未接続。
これは研究オーケストレーションの運用条件で、既存 DSR/PBO/Hard Gate を変更するものではない。

## 容量・inode・IOPS と計測

C の必要容量は retained raw + 最大受信中 raw + publication 用の同時コピー + metadata +
quarantine prefix + backup/復旧余裕 + reserve。hardlink を使う本体と、copy で重複する backup の
占有量を分ける。既定 raw 16 MiB、行 256 KiB、10,000行、store 1 GiB、reserve 16 MiB は
配置の初期制限であり、本番必要量ではない。

D はイベント数 E と serialized bytes B、FS block K に対して概ね
`E * ceil(B/K) * K + directory/backup`、inode は約 E。
optimizer probe 回数 `Q = 2 * steps * EML_node_count * n_init` に start/finish の 2Q event が
加わる。Q は N_raw ではない。設定比較と候補数も別に見積もる。

probe は 4 KiB×64 file の file fsync と directory fsync、logical同期組/秒、利用可能 byte/inode を測る。
結果は監査 JSON に保存する。共有された仮想 FS の短い合成測定で、物理デバイス IOPS、実データ
throughput、本番 p95、必要 CPU/RAM/保持期間を推定する根拠にはしない。
実配置で容量・inode reserve、最大連続書込、fsync p95/p99、復元時間、RPO/RTO を測るまで rollout は Hold。

## 秘密・原本の漏洩防止

receipt と CLI には検査 code／hash／件数を使い、任意 payload・source path・例外 message・DSN を出さない。
D の credential-like key 拒否は補助で、全任意文字列の秘密を自動発見する機能ではない。
raw は元情報を含むため owner 専用権限、最小アクセス、承認済み暗号化 backup、PR への持出し禁止が必要。
認証情報をデータとして送らず、誤送時は隔離・公開停止・credential rotation を別手順で行う。
今回は実原本・秘密値を受領／公開していない。

Go: 合成ローカル復元試験と運用設計。Hold: off-host backup、外部固定、WORM、実機障害復旧、
本番 worker/Exporter 接続、実データ研究。新 Hard Gate・NOTE-008 の採用評価は実データ/N/returns 待ち。

## 今回のCloud中断後に追加確認した範囲

実行transport切断後、環境復帰時にsource/backup/restoreの原本集合・receipt/event hashを
中断前local checkpointと照合し一致した。停止した専用PG containerを再開し、固定capture16件の
hash replayと、既存の意図的破損fixture1件の拒否を確認。全体PGも2217成功/7件deselectedを再現した。
生ログとscriptを回収してGitHubへ追加保存した。これはoff-host/電源断/媒体喪失の検証ではなく、
全tail削除やowner攻撃への外部anchor保証も追加しない。
