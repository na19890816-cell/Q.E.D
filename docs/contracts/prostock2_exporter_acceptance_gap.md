# Phase 6B — 実 Exporter 受入条件と raw JSON artifact 提案

2026-10-18。実 EC2・本番 DB・cron・取引系には接続していない。
本書は利用者が示した所在情報との契約照合であり、実原本の bytes を検証した記録ではない。
実ファイルも正式 Exporter manifest も未受領のため、実結合は **Hold**。

## 原本の所在と判定可能な範囲

| 提示された原本候補 | 未確定事項／受入条件 |
|---|---|
| `screen/snapshot_*.json` | 単一 JSON document、JSONL、pretty JSON のどれか未確認。拡張子・名前・件数から決めない |
| `candidates_log.jsonl` | 物理行、末尾 LF、UTF-8、重複 key、NaN/Inf、行長、件数、全候補性を未確認 |
| OVX・JVRAM 履歴 | 形式、系列定義、単位、改定版、公表／計算／available_at の根拠が未取得 |
| 信用残 manifest | manifest 本文、対応する原本、対象日、観測版、hash の定義と完全性が未取得 |
| 日足 OHLCV 原本 | 未保存。特徴量 snapshot から原本や原市場 hash を復元したと扱わない |
| 過去 code/policy | 未取得。現在版を過去版として代入しない |
| 過去 availability | 一部不完全。ファイル mtime、受信時刻、翌営業日で補完しない |

91 snapshot／446 銘柄は利用者提示の所在情報で、直接観測した行数や試行数ではない。
`446 × 日数` を期待 universe、全探索 N、独立観測数へ変換しない。
regime／candidate を既存 feature に埋め込まない。数値 regime 指標が既存 feature の意味を
満たすかは正式データ辞書で判断し、カテゴリ・遷移・選抜履歴は専用契約の要否を別レビューする。

## 実原本を受領してから行う検査

1. 正式提供者・範囲・承認・秘匿性・保持条件を記録し、認証情報を含まない転送経路を指定する。
2. 変更しない原本 bytes に対して SHA-256 と byte 長を計算し、producer の完了 manifest と照合する。
3. manifest の宣言した serialization に従って検査する。single JSON は全体を一つの文書として
   strict decode する。JSONL は物理 LF 境界・最終 LF・各行 object・strict JSON を検査する。
   行指向 receiver へ単一文書を流して、銘柄レコードを検証済みと呼ばない。
4. producer manifest SHA と receiver receipt の raw SHA／size／契約版／取得時刻を対応づける。
   hash 不一致、不完全 manifest、転送中断、同一 ID 別原本は隔離し、完全な同一原本は再送可能とする。
5. `market_date` は何の市場・対象日かを辞書と原本で確認する。filename、mtime、UTC 日付、
   日本市場の営業日、米国公表日を同一視しない。全 timestamp は timezone 付き、保存は UTC。
6. 同一日付の複数観測は source revision／実観測時刻／raw hash を保持し、最新値で上書きしない。
   revision 順序が不明なら不明のまま保存し、利用可否は Hold。
7. 正式 universe/calendar から日付×銘柄×測定の期待集合を独立に固定し、欠落・余剰・重複を照合する。
   446 という数や実ファイルに現れた生存候補だけから期待集合を作らない。
8. 値の欠損、source 未取得、時点不適格、gate 拒否を区別して保持する。unknown を 0 にしない。
9. 原市場 bytes、依存 evidence、当時の code/policy、availability の根拠が未解決なら原本保管のみ。
   保存成功／入力 ready／ACK は研究適格性を付与しない。

single JSON と JSONL 1行1object は byte grammar が重なる場合もある。構文だけで意図を推測せず、
producer manifest の serialization とデータ辞書が一致して初めて論理レコードの単位を決める。
合成 pretty JSON は現行 Task C で隔離され bytes が変化しないことを probe で確認した。
これは実 snapshot 形式の証明ではない。

## raw JSON artifact 契約案 — 未実装／受入許可ではない

既存 `prostock2_audit_v1` record と strict JSONL transport v1 は変更しない。
single JSON 原本が正式に提供される場合は、新しい契約 ID
`urn:qed:artifact:raw-json:1` を合意してから receiver とテストを別 PR で実装する。
本 Phase の成果は設計案で、現 receiver がこの形式を受け付けるとの宣言ではない。

| 提案フィールド | 意味／不明時の扱い |
|---|---|
| `artifact_schema_version` | integer 1。既存 record/transport 版と独立 |
| `artifact_id`, `export_id`, `source_system` | 新たな export を安定識別。過去 research run と同一視しない |
| `serialization` | `single_json_document` 固定。複数文書や JSONL を受け入れない |
| `media_type` | `application/json`。圧縮するなら別契約と展開前後の制限が必要 |
| `raw_sha256`, `size_bytes` | 改行・空白・文字コードを含む原本 bytes。受信側で再計算 |
| `artifact_kind` | snapshot / candidate-log / regime-history 等の分類。研究レコード型ではない |
| `exported_at` | 実際の export 固定時刻。timezone 必須、UTC。過去 captured_at の代用不可 |
| `historical_captured_at`, `available_at`, `market_date` | 根拠と scope を別記。未知は null、理由必須 |
| `source_revision`, `code_commit`, `policy_hash` | 実際の対応版か null。現在版／ゼロ hash で補完しない |
| `universe_manifest_sha256`, `calendar_version` | 正式な期待集合・calendar の参照か null |
| `artifact_role` | original / derived を区別 |
| `derived_from` | derived の場合、原本 raw SHA、変換 code commit、仕様 hash、変換設定 hash が必須 |
| `logical_record_count` | single document の論理単位は辞書受領前は null。物理行数を件数に代入しない |
| `provenance_gaps` | 不足を列挙。空配列でも自己申告だけで研究許可しない |

original は変換前 bytes のまま保存する。derived は別 artifact ID と raw SHA を持ち、
元の hash へ有向に結び付ける。pretty-print、改行追加、数値型変換、JSONL 化は全て派生物。
同一日・別観測版を一意に保つ natural key と original/derived の再送・競合規則を追加レビューする。
Task C と同等の容量／depth／重複 key／nonfinite／原子保存／fsync／receipt／復旧の試験が必要。
この案への無断移行や既存 v1 の enum 拡張は行わない。

## Exporter 側の必要改修と次の判定

- 完了後にのみ原本 hash・byte 長・serialization・安定 export ID を固定し、転送中ファイルを公開しない。
- 原本と派生物の保存先・保持期限・hash 対応を分け、未転送／未確認原本を期限だけで削除しない。
- timezone、market_date、観測版、availability 根拠、辞書／universe／calendar を提供する。
- code/policy と原市場依存を将来の実行開始から保存し、過去不明値は復元したと主張しない。
- 正式 receiver durable ACK を再照合してから、保持・backup policy と承認に基づき削除を別判断する。
- snapshot／候補／regime／信用残の匿名化標本と公式 manifest を、正式な提供・承認後に相互検証する。

Go は受入条件の設計まで。実 bytes、辞書、manifest、期待集合、時刻根拠の未取得により実受入は Hold。
OHLCV、全探索 N、cost 後 returns が揃うまで NOTE-008 採用評価・新 Hard Gate は Hold。
