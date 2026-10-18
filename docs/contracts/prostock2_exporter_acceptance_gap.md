# Phase 6B — ProStock2実原本との契約差分
2026-10-18。正式な実原本/manifest/辞書は未受領。EC2、本番DB、cron、取引へ接続していない。
本書は利用者提示の所在情報との照合で、実bytesを直接検査した記録ではない。

## 原本別の受入条件
| 原本候補 | 取得して確認する事項 |
|---|---|
| screen/snapshot_*.json | single JSON/pretty JSON/JSONLの宣言と実bytes。拡張子だけで決めない |
| candidates_log.jsonl | UTF-8、物理LF、最終LF、duplicate key/NaN/Inf、行長/件数、全候補性 |
| OVX/JVRAM履歴 | serialization、系列/単位/頻度/改定、公表/計算/利用可能時刻 |
| 信用残manifest | 本文と対応原本、対象市場/日、観測版、hash定義と完全性 |
| 日足OHLCV原本 | 未保存。特徴量から原本や原市場hashを復元したと扱わない |
| 過去code/policy | 未取得。現在HEAD/設定で埋めない |
| 過去available_at | 一部根拠不完全。mtime/受信時刻/翌営業日で推測しない |

91 snapshot/446銘柄は所在情報であり直接確認した観測数・全探索Nではない。
期待universeは正式な当時のuniverse/calendar/測定集合から独立に固定し、
446という数やファイルに残った生存候補から作らない。欠落/余剰/重複を保存する。

## 正式提供後に行う検査
1. 提供主体、承認、範囲、秘匿性、保持条件を記録する。認証情報を原本/PRへ含めない。
2. 元bytesを変更せずraw SHA-256とbyte長を計算し、producer完了manifestと照合する。
3. manifestに宣言したserializationでstrict検査する。single JSONは全体を一文書としてdecodeする。
   JSONLは各物理行のobject、末尾LF、duplicate key/nonfinite/長さ/件数を検査する。
4. producer manifest SHAとreceiver receiptのraw SHA/size/契約版/実受信時刻を対応づける。
5. timestampはtimezone根拠を取得してUTC保存。market_dateの市場/対象日/根拠を別に確認する。
   filename、UTC日付、日本市場営業日、米国公表日を同一視しない。
6. 同一日付の複数観測はrevision、実観測時刻、raw SHAで区別する。最新値で上書きしない。
   revision順序不明は不明として保存する。
7. 欠損、拒否、時点不適格、未取得を区別する。不明値を0へ補完しない。
8. 同一原本再送は同じhash/manifestで冪等処理、同一ID別原本は競合として隔離する。
   部分原本/転送中断/manifest不完全は原本を保持して完全再送を求める。
9. 原市場bytes、依存、当時のcode/policy、availability根拠が不足すれば保管のみ。
   ready/保存成功/ACKから研究利用を許可しない。

single JSONとJSONLの1行objectはbyte grammarが重なることがある。
構文だけで論理単位を推測せず、producer宣言と正式辞書を優先する。
合成pretty JSONを現Task Cで隔離し、元bytes不変を確認したが、実snapshot形式の証明にはならない。

## raw JSON artifact契約案 — 未実装
既存record v1とstrict JSONL transport v1を変更しない。
single JSON正式提供時は別ID urn:qed:artifact:raw-json:1 を合意し、receiverを別PRで実装する。
本書の提案を現receiverの対応済み形式と扱わない。

| 提案項目 | 意味 |
|---|---|
| artifact_schema_version | 1、record/transportの版と独立 |
| artifact_id/export_id/source_system | exportを安定識別。過去research runの代用ではない |
| serialization/media_type | single_json_document/application/json |
| raw_sha256/size_bytes | 空白/改行/文字コードを含む原本bytes |
| artifact_kind | snapshot/candidate-log/regime-history等、研究record_typeとは別 |
| exported_at | 実export固定時刻。timezone必須、UTC |
| historical_captured_at/available_at/market_date | 根拠とscope。未知はnullと理由 |
| source_revision/code_commit/policy_hash | 実際の対応版またはnull。仮値を入れない |
| universe_manifest_sha256/calendar_version | 正式期待集合/calendar参照またはnull |
| artifact_role | original/derived |
| derived_from | derivedには原本SHA、変換code commit、仕様hash、設定hashを必須 |
| logical_record_count | 辞書未取得ではnull。物理行数を観測数にしない |
| provenance_gaps | 不足一覧。空でも自己申告だけで研究許可しない |

originalを整形/改行追加/JSONL化/数値型変換しない。
変換物には新artifact ID/hashを付け、原本hashへ結び付ける。
圧縮、上限、depth、duplicate key、nonfinite、原子保存、receipt/復旧、
original/derivedの再送と競合を別の仕様・validator・テストで固定する。

regimeカテゴリ/遷移とcandidate選抜履歴はfeatureへ無理に詰め込まない。
数値regime指標のfeature適合も正式な定義/単位/依存/時点根拠で判断する。

## Exporter改修
- 完了後だけraw hash/size/serialization/安定IDを固定し、転送途中fileを公開しない。
- 当時のuniverse/calendar、code/policy、source revision、availability根拠を保存する。
- 原本/派生物/依存証拠の保管とhash対応を維持する。
- receiver durable ACKを認証した経路で照合し、転送ACKと区別する。
- 未転送/未確認原本を期限だけで削除しない。保持解除は別承認とする。
- 正式な匿名化標本、データ辞書、manifest、取得範囲を提供して相互検証する。

実bytes/manifest/期待集合/時刻根拠待ちで実受入はHold。
OHLCV、全探索N、cost後returnsが揃うまでNOTE-008採用評価と新Hard GateはHold。
