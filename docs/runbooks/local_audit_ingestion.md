# Phase 5R Task C — 独立したローカル監査受入

## 契約と接続境界

本機能は明示的に呼び出すローカル Python API である。DB、HTTP、EC2、cron、売買、
FROST、DSR、PBO、昇格へ接続しない。PR #4 の record v1 は変更しない。
[C-0 所在照合](../reports/phase5r_c0_contract_alignment_2026-10-17.md) は利用者提示情報に基づき、
実ファイルを受領・検証した記録ではない。

[transport manifest v1](../../qedschema/contracts/ingestion_manifest_v1.schema.json) は
record の `schema_version: 1` と独立した版である。必須値は以下。

| フィールド | 意味 |
|---|---|
| `transport_schema_version` | strict integer `1` |
| `source_system` | `prostock2` |
| `transfer_id` | 生産者が固定する安定 ID。内容／検査条件が異なる再送には使い回さない |
| `record_contract` | `prostock2_audit_v1` または `legacy_audit_archive_v1` |
| `raw_sha256` | 改行・空白を含む原ファイル bytes の SHA-256、lowercase hex 64 |
| `size_bytes`, `record_count` | 完了済み原本の正確な byte 数と JSONL 行数。推定値不可 |
| `expected_observations` | v1 は重複なしの期待集合。archive は null |

期待集合の各要素の順序は `source_system, run_id, record_type, symbol, data_date,
universe_version, measurement_id`。受信 batch 内の実集合と完全一致を求める。
期待集合自体から漏れた銘柄／日付は検知できない。Exporter が独立した universe と calendar に
基づく期待集合を提供する必要があり、446 という数から銘柄集合を推測しない。
manifest 全体の canonical SHA も固定し、同じ transfer ID で期待集合や契約を変更することを拒否する。
JSON Schema は構造と既定 profile を表す。strict integer（Python bool／float 不可）、
byte 長、重複 JSON key、意味論は Python validator と受信検査を必ず併用する。

archive は旧原本の保管経路であり、新しい研究レコードの契約ではない。JSONL は UTF-8、
各行が strict JSON object、重複 key／NaN／Inf／overflow／空行なし、最終 LF 必須とする。
有限 float は archive のみに許容する。record v1 の decimal-string 規則は従来の validator が適用する。
pretty JSON、CSV、圧縮など未対応原本は bytes を隔離し、改行変換等を加えて元原本と呼ばない。
独立 regime／candidate の将来契約と archive 受信を混同しない。

## 状態と許可

`RECEIVED` は実受信開始、`INTEGRITY_VERIFIED` は raw hash と長さ一致、
`CONTRACT_VALIDATED` は record v1・依存閉包・期待集合の検証成功を表す。
入力の `ready/missing/rejected` とは独立している。正常 v1 も最終状態は
`PROVENANCE_INCOMPLETE` とする。archive の strict JSON 成立は record v1 成立と記録しない。
破損・不完全・矛盾・競合は `QUARANTINED` とし、その batch から新規 record claim を作らない。

`RESEARCH_ELIGIBLE` は予約状態であり、今回その状態を付与する API は実装しない。
manifest に追加の許可フィールドを入れると拒否する。ready の自己申告、成功 ACK、
ファイル名、hash 一致によって研究利用を許可しない。原証拠の真正性・未申告依存・全期間網羅・
過去 availability・code/policy の一致はこのソフトウェアだけでは証明できない。
将来の独立した provenance 審査と版管理された許可手順は Hold。

## 保存手順と再起動

`LocalAuditStore(root).receive(binary_stream, manifest)` を呼ぶ。`root` は所有者管理下の
専用ローカル POSIX directory とし、他ユーザーに書き込みを許可しない。親 directory は事前に
作成・永続化しておく（未作成の祖先は API が自動作成しない）。標準ライブラリのみを使う。

1. flock により同一 store の処理を直列化し、未完了 intent を復旧・既存 receipt を検証する。
2. manifest と実際の受信開始 UTC 時刻を持つ intent を、専用一時ファイル→fsync→
   exclusive hardlink→directory fsync で固定する。入力の時刻を receiver 時刻に代用しない。
3. bounded stream を一時ファイルへ保存し fsync。size/hash、行数／行長、strict JSON、
   既存 v1 validator、同一 batch の依存 existence/evidence hash/time/ready、期待集合を検証する。
   `checks.dependencies` はこの閉じた record 依存の確認である。`market_data_hash` の原市場
   artifact は別の `checks.source_artifacts` で、過去に同 store へ正常受信済みの raw SHA と
   実在 bytes の再hashを照合する。未受信／隔離原本や null は未解決として receipt に記録する。
   別 transfer の archive として原市場 JSONL を先に提供できるが、hash 一致は当時の由来・
   網羅性の証明にはならず、全 artifact が揃っても独立 provenance 審査を省略しない。
4. 原本を `blobs/<raw_sha256>.raw` に同じ原子的手順で固定。既存 bytes は上書きせず再検証する。
   不適合の原本は `quarantine/<attempt>.raw` に同一 inode のリンクを残す。
5. record identity と natural key を既存の terminal receipt から再構築して競合検査する。
   receipt を原子的に固定してから戻る。receipt commit が新しい record claim の公開点となる。

同一 transfer ID・raw hash・manifest hash の再送は raw／record を増やさず、新しい実受信時刻を持つ
receipt で `duplicate_of` を参照する。異なる transfer ID の部分重複は、同一 record ID・同一 evidence
を再利用し新規 record のみ claim する。同一 record ID の異なる evidence、同一 natural key の別 ID、
同一 transfer ID の別原本／manifest は batch 全体を隔離する。batch 内の重複は既存 v1 規則で拒否する。
byte 表現が異なる原本は別 raw blob であり、canonical record が同じなら record は再利用できる。
この冪等処理は評価試行 N を追加する処理ではない。

`recover()` は terminal receipt のない intent を列挙する。残存一時原本が manifest の正確な
size/hash を満たす場合だけ再検証して固定する。不完全なら prefix を隔離し `INTERRUPTED_TRANSFER`
receipt を固定する。元の実受信時刻を保ち、復旧時刻を完了時刻として別記録する。
receipt 固定後の中断は既存 receipt を検証して後片付けする。source は変更せず、送信者は
同一 manifest と完全原本で再送できる。byte-offset 再開や不完全 prefix の連結は行わない。
metadata publication の未完了 staging は診断のため保持し、完了したデータと解釈しない。

## receipt と保存確認

receipt は実受信 UTC 時刻、完了時刻、raw SHA／宣言 SHA、完全転送の有無、実保存 byte 数と
実観測行数、契約版、manifest SHA、状態履歴、各検査結果、隔離理由、重複／新規件数、
record claims、receipt 自身の canonical SHA を含む。不明な件数を宣言値やゼロで代用しない。
不正 manifest の詳細や source 例外文字列、本文、認証情報は記録・出力しない。
不正受信も実際に保持できた bytes の hash を記録する。prefix の hash を完全原本の hash と呼ばない。
`record_count` は strict JSONL 検査前／失敗時には null とし、未検証の行分割数は
`observed_line_count` として別記する。`stream_complete` は EOF 到達、`raw_complete` は
さらに宣言 size/hash との一致まで確認したことを表す。不明 manifest の宣言件数は null。

`acknowledge(receipt_id)` は raw／manifest／receipt を再読込して検証したコピー保存確認を返す。
quarantine／不完全原本には ACK を発行しない。ACK は raw、size、manifest、receipt SHA を束縛し、
`research_eligible=false`、`producer_deletion_authorized=false` を明示する。
producer 側は認証された別チャネルで ACK を保存・照合し、バックアップ／保持 policy と人間の承認を
確認してから削除を別途決める。保存期限だけで未転送・未確認原本を消してはならない。
今回 producer の削除処理や自動転送を実装しない。

## 資源、容量、保証の限界

既定上限は raw 16 MiB、1行 256 KiB、10,000 行、manifest 4 MiB、store 1 GiB、
空き領域 reserve 16 MiB。JSON Schema はこの既定 deployment profile を表す。
受信側 `Limits` の変更は manifest 上限と Exporter profile の合意を伴う別の配置設定とする。
raw と一時保存の同時存在・metadata・復旧余裕を確保する。処理は raw 上限内でメモリ解析し、
既存 receipt/blob を再検証するため履歴量に比例して I/O が増える。大量運用向け索引・GC・転送 daemon
は未実装。実データ量・到着頻度・保持期間は未取得なので、本番容量はまだ確定できない。

OS の空き容量と設定 quota を確認し、write/fsync 失敗は成功扱いせず固定 code の StorageError にする。
容量不足時に失敗 receipt 自体を書けないことがある。その場合 intent／partial を残して fail-closed とし、
空き領域復旧後に `recover()` を実行する。ディスクが完全に失われた場合の receipt 永続性は保証しない。
単一ローカル FS の hardlink、flock、directory fsync が必要。NFS、Windows、複数 host、
ディスク／FS の虚偽 fsync、電源喪失時の実ハードウェア、媒体故障、バックアップ復元は未検証。
ローカル権限者による原本／全 receipt の同時改ざん・削除は hash chain／hash だけでは防げない。
署名付き外部 checkpoint、WORM、暗号化、長期保管の独立監査は将来の運用設計事項。
stream の永久 block に対する deadline／cancel は API 内にない。受信中は同じ store の flock を
保持するため、他 receive／recover／ACK も待つ。生産者から独立した worker と資源で実行し、
外側で timeout／終了後に recover する必要がある。その daemon／資源制限の本番運用は未実装。
今回の合成試験は品質検証であり、実履歴や統計的アルファ・採用可否の証拠ではない。
