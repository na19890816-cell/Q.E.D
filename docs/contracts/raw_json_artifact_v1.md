# Raw JSON artifact v1 — 原本保管契約

2026-10-18。`urn:qed:artifact:raw-json:1`。JSON Schema は
`qedschema/contracts/raw_json_artifact_v1.schema.json`、意味論の純関数は
`validate_artifact_manifest` と `validate_document`。
**実 ProStock2 原本には未適合判定**。実標本・辞書・Producer 合意後に互換性を再審査する。
既存 record v1 と strict JSONL transport v1 は変更しない。

## 範囲と manifest

raw は BOM のない strict UTF-8 の **1つの JSON object または array**。
pretty JSON、改行、空白、数値表現を保存前に書き換えない。末尾 LF は任意。
重複 key、NaN/Inf、float overflow、孤立 surrogate、後続文書、途中切断は拒否する。
JSONL の複数文書は対象外。1行 object の grammar が重なる場合は Producer 宣言を必要とし、
serialization を自動推定しない。候補・regime の研究レコード意味論は定義しない。

manifest は全 field 必須、追加 field 禁止。原本の JSON payload にはスキーマを捏造しない。

| field | 型と意味 |
|---|---|
| contract_id / artifact_schema_version | 固定 ID / integer 1。transport/record の版とは独立 |
| source_system | `prostock2` |
| artifact_id / export_id | 各128字以内の ASCII ID。1 export は1 artifact。再送は同じ manifest |
| artifact_kind | snapshot / jvram_history / ovx_history / regime_history / other。分類のみ |
| serialization / media_type | single_json_document / application/json |
| exported_at / complete | 実 export UTC 時刻 / boolean true。過去 availability の代用不可 |
| raw_sha256 / size_bytes | 改行・空白を含む原本 bytes の lower hex SHA-256 / 正の整数 |
| artifact_role / derived_from | original は参照なし。derived は1件以上の親参照 |
| provenance | 下記の全 field を保持。未知は明示 null |
| provenance_gaps | null field 名を全て列挙し、追加の不足も安全な code で保持 |

provenance は market_date、historical_captured_at、available_at、availability_evidence_sha256、
source_revision、code_commit、code_content_sha256、policy_hash、universe_manifest_sha256、
calendar_version、logical_record_count。過去不明値を zero、mtime、現在版で補わない。
論理件数は辞書に基づく Producer 宣言で、Receiver は件数検証済みとしない。
market_date は実在する日付。時刻は timezone 必須で wire は UTC `Z`、小数秒最大6桁。
historical_captured_at / available_at は exported_at 以下。available_at の申告には evidence hash 必須。
これは時点整合性の必要条件で、hash 先の内容や現実の公表・市場到達時刻を証明しない。
Receiver の受信時刻は実際の現在 UTC を記録し、原本時刻へ転記しない。

derived_from は各親の raw_sha256 と transformation_code_sha256、spec_sha256、settings_sha256。
重複親・自分自身への参照は禁止。親は同じ artifact store に先に完了保管された非隔離 artifact
でなければ拒否する。親がさらに derived であれば chain を保持する。
未提供の original を参照する匿名化標本は、この厳格な参照閉包を満たさないため隔離される。
標本を original と偽称して迂回しない。正式原本を持ち出せない場合は提供者側での確認に留め、
安全な標本専用契約を別途合意する。変換の正しさ・提供者の権限は独立審査が必要。

同じ市場日でも観測版ごとに異なる artifact/export ID で保存する。市場日を上書き key にしない。
同じ artifact ID または export ID の別 manifest は、同じ raw でも競合として隔離する。
未完了・不正・親未着の隔離 receipt は ID を占有せず、修正後の完全再送ができる。
受理済みの同一 manifest 再送は同じ raw blob を再利用し、新しい receipt に duplicate_of を残す。
内容が同じ別 ID の再送も raw blob は一つだが、研究試行数には一切計上しない。

## 永続化・receipt・復旧

PR #5 `35d670f` の LocalAuditStore の POSIX primitives と intent/recovery protocol を再利用する。
JSONL receive/record claims は呼び出さない。専用 root と契約 marker で保存し、既存非空 store は
採用しない。既存 v1 ファイルは byte 単位で不変。private primitives の依存は将来の refactor 時に
両 receiver の障害試験を必要とする。root の親は予め provision/durable であること。

1. flock 下で復旧を実行。manifest の純関数検証後、canonical manifest と checksummed intent を
   exclusive publish。raw SHA は canonical manifest hash と異なり、原本 bytes のみから計算する。
2. bounded binary stream を O_EXCL/O_NOFOLLOW/0600 の pending に保存、flush/file fsync。
3. raw blob を一時 file＋file fsync＋exclusive hardlink＋directory fsync で公開。上書き禁止。
4. raw size/hash、JSON grammar、親の実在/hash、ID 競合を確認。失敗した prefix/raw も隔離して保持。
5. checksummed receipt を同じ原子 protocol で保存。**durable terminal receipt が commit point**。
   既存 file の再利用時にも file/directory fsync を完了してから成功を返す。
6. ACK は raw/manifest/receipt を再読して hash と保存を確認。transport ACK と区別する。
   `producer_deletion_authorized=false` / `research_eligible=false`。
7. restart は checksummed intent/receipt を検査。size/hash が完全一致の pending のみ再検証し保存。
   不完全 prefix は隔離。offset 再開は実装せず、Producer が同一原本を完全再送する。

receipt は取得/完了 UTC、契約版、manifest/raw hash、byte 長、artifact/export ID、role/親 hash、
検査結果、状態履歴、理由 code、重複元、復旧有無、receipt SHA を保持する。
`RECEIVED → INTEGRITY_VERIFIED → CONTRACT_VALIDATED → PROVENANCE_INCOMPLETE` の
CONTRACT_VALIDATED は保管文法の検証のみ。原本の銘柄・日付・辞書・利用可能性を検証した意味ではない。
失敗時は QUARANTINED。RESEARCH_ELIGIBLE の付与 API はなく、偽造された昇格状態も読み取り時拒否。
ready/missing/rejected という既存レコード状態には自動変換しない。

## 制限と保証外

既定は raw 16 MiB、manifest 4 MiB、depth 64、JSON node 100,000、store 1 GiB、reserve 16 MiB。
line/record 制限は既存 Limits を継承するが artifact の物理行数・論理件数には適用しない。
上限超過時は最大 raw 上限+1 byte の prefix だけ保存し、不完全として隔離する。
巨大原本は事前サイズ調査後に資源審査する。圧縮、ネットワーク転送、daemon、認証は実装しない。

容量不足・inode 枯渇・fsync/write 失敗では成功 ACK を返さない。失敗 receipt も書けない場合は
intent/pending と安全な error code のみになり、容量回復後 recover が必要。永久停止 stream は
global flock を占有するので、独立 worker の外側 timeout/kill/recover が必要で未接続。
local POSIX の flock/link/fsync を仮定し、NFS/Windows/実機停電/媒体故障を保証しない。
所有者による全ファイル再生成・全削除、外部署名、WORM、off-host backup、外部 tail 固定は保証外。
receipt 全走査は履歴量に比例する。実資源量・本番 throughput は未測定。

原本には秘密が含まれ得る。owner 専用 file、限定された root、別管理の認証・暗号化・保持手順が必要。
API は payload/source exception を receipt/log に書かないが、秘密を全自動発見する機能はない。
原本や実 hash の GitHub 公開は行わない。研究・FROST・実売買へは未接続。
