# Phase 7C/D/E — 相互受入・前向き証拠・運用前審査

2026-10-18。実原本、正式 Producer manifest、EC2 計測値、承認された接続は未取得。
実相互試験・本番接続・NOTE-008 採用評価は **Hold**。

## Producer / Receiver 相互試験

| 試験 | 合成ソフトウェア範囲 | 実 Producer / EC2 |
|---|---|---|
| 完了 manifest / SHA / byte 長 / durable receipt | ローカル fixture で検証 | 未提供・未検証 |
| 再送 / ACK 喪失 / 1byte 改変 / 同一 ID 別版 | ローカルで検証 | 未検証 |
| 転送中断 / SIGKILL / restart / fsync 失敗 | ローカル POSIX で検証 | 実転送・電源断は未検証 |
| 日付・銘柄・観測版・universe / 不適格 | 保管側は値を改変せず保持。意味論の適合判定なし | 辞書・期待集合・時刻根拠待ち |
| crisisEngine / 売買への影響 | receiver にその接続・呼出しなし | 非接続。実機無影響試験と呼ばない |

正式に標本を受領した後、承認・hash/size・serialization・辞書・時刻根拠・期待集合を固定し、
sample request の順で読み取り専用検査を実行する。single JSON/JSONL を推測しない。
提供者側でも同じ完了 manifest と receiver ACK の raw/manifest/receipt hash を照合し、
ACK 喪失で同一原本を再送する。原本保持解除は別の承認・backup 確認を必要とし、
受信成功を研究適格としない。未知の過去 available_at/code/policy/N を補完しない。

## 前向き研究証拠

実行開始より前に独立した run registry に run/search/family、予定設定、audit 必須、code/policy/input
hash、universe/calendar、観測 cutoff を固定する。実行監査 spool 自体から予定集合を作らない。
各 market_date の市場・観測 UTC 時刻・利用可能時刻の根拠を分ける。
米国公表の IANA timezone/DST、公表・取得・計算・embargo と依存 evidence の最大時刻を保存し、
日本市場で最初に使用できた意思決定時刻を calendar と照合する。mtime では代用しない。

| 証拠 | 将来固定する内容 | 過去の扱い |
|---|---|---|
| code | 実 commit、dirty なら実内容 hash＋依存 lock | 不明は null。現在版を代入しない |
| policy | 実本文＋raw/canonical の hash 規則＋版 | hash のみで本文確認済みとしない |
| universe / 市場原本 | 版・全銘柄集合・raw OHLCV/公表原本/改定 | snapshot から復元しない |
| 全候補 | 生成前後・top_k 前除外・棄却・非有限・失敗・中断 | 非生存候補を推測生成しない |
| 探索設定 | 重み/閾値/family/parent と開始時設定、終了時 count を別固定 | 設定比較を N=1 で代用しない |
| N_raw | ADR-002 の S1/S2/S3 所属・重複/再評価/派生を照合 | 未知 N は null、採用停止 |
| returns | 時刻付き net-cost returns、約定・手数料・slippage/欠損 | 合成 returns で採用判断しない |
| OOS | 探索/検証/最終確認の時系列分割、purge/embargo、固定 seed | 同じ OOS で基準を反復最適化しない |

Task D journal は観測用で、台帳への二重登録を行わない。S1/S2/S3 N_raw 規則、DSR/PBO、
FROST score/Gate/promotion は変更しない。新しい raw artifact の数は研究試行数ではない。

## 監査未指定 run と fail-closed の接続前検証

`scripts/audit/phase7_prospective_audit_probe.py --tree <PR6を含む隔離checkout>` は
独立した**合成** registry を持ち、callback 前に durable RUN_STARTED の context/settings を照合する。
audit=None、未登録 run、完了済み attempt の再利用、破損 start は callback 前に拒否する。
registry にあり journal がない run を検出し、研究 release を停止する。
実 journal の publish failure で TRIAL_STARTED が保存できなければ評価を開始せず、writer を閉じる。
これは前向き launcher の局所モデルで、既存 EML/FROST の default `audit=None` を変更しない。
registry にも登録されなかった実 run の全体検出、独立 registry の永続・認証・外部固定、
実 scheduler との原子的接続は未実装。監査の完全網羅を保証せず、運用接続は Hold。

## 運用前の人間による審査項目

EC2 の調査は別途正式な提供・承認が必要。本 PR は接続・cron・SSH 鍵・DB 導入を行わない。
以下は測定計画であり測定結果ではない。

1. **朝処理3営業日**: 各処理開始/終了 UTC、同時処理、CPU utilization/credit balance/usage/
   surplus、RAM/swap、I/O wait、disk throughput/IOPS/queue、fsync p95/p99、容量/inode を取得。
   同じ時間帯の既存 crisisEngine の遅延・失敗・許容値を所有者が比較する。閾値は実測後に承認。
2. **増分容量**: raw＋全改定＋pending＋quarantine＋manifest/receipt＋audit event＋backup の
   日次増加と inode 数を測定。`必要量 >= 保持日数×実測日次増分 + 最大同時受信/公開コピー +
   復旧領域 + reserve`。全走査コストも計測し、データ未取得時に本番容量を断定しない。
3. **別障害領域 backup**: quiesce した原本/metadata/全 event/独立 registry を manifest で固定し、
   認証・暗号化・鍵分離・保持・アクセス監査を整備。receipt/event tail と manifest hash を外部固定。
   新しい媒体への復元と参照閉包・欠落末尾・未完了の検出を試験し、RPO/RTO を承認する。
   本実装のローカル copy 復元を off-host/WORM/外部署名の成功と記載しない。
4. **ACK 喪失**: transport ACK と durable ACK を別保存。同一 manifest の完全再送で確認する。
   未転送・未確認・隔離原本を TTL だけで削除しない。ACK 自体は削除権限を持たない。
5. **隔離・停止**: 容量/inode不足、write/fsync failure、hash/manifest/tail不一致、未知 N では
   Q.E.D 研究 worker を停止。ProStock2 の cron・売買管理を巻き込まない独立プロセス・資源上限を
   準備する。stream timeout は外側 worker に必要。自動 cleanup/再試行 storm を導入しない。
6. **秘密・改変**: 提供者側の機密審査、限定権限、改変前後 hash、credential を含まない receipt、
   原本を PR に出さない手順、誤送時の隔離・credential rotation を整える。

Go: ソフトウェア契約・合成ローカル試験まで。Hold: 実標本相互試験、実資源審査、独立 registry、
off-host/実障害復旧、本番接続、親スキーマ実結合7件、NOTE-008 採用、新 Hard Gate。
