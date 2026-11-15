# Phase 8C — 実原本の正式提供後の受入手順

レビュー改訂案: 2026-11-15。基準は PR #9 HEAD `7edb3f61263cc003b390fa801863e62e44984b16`。
実 bytes、正式 Producer 完了 manifest、正式 universe、取得 symbol の原証拠は未受領。
今回の範囲は下記①②と③の合否基準の固定まで。③の実相互試験は正式提供待ちで **Hold**。
過去の合成試験を今回の実受信成功と記録しない。本番/EC2/DB/cron/売買へ接続しない。

| 順序 | 作業単位 | 完了条件 |
|---|---|---|
| ① 先に固定 | 原本ごとの受入経路・manifest/receipt の対応 | serialization 宣言に従い、元 bytes を変更せず個別に追跡できる |
| ② 受領前に固定 | PR #9 のコード外で照合する根拠と記録先 | candidates / margin / issuer・銘柄別上場期間について、未確認も記録できる |
| ③ 正式提供後 | 連続2市場日の snapshot を1つの実相互試験 case として検証 | 下記③の全必須項目と Producer 側照合を記録する。同日別観測版は届いた場合に追加 |

受領成功後も receipt の状態は **PROVENANCE_INCOMPLETE**、`research_eligible=false`。
受入・相互試験の合格は研究利用許可ではない。既存契約、DSR/PBO/FROST/Hard Gate/昇格は変更しない。

## 必要資料

- 承認識別子、提供者、原本/匿名化派生の区分、機密区分、保持条件。raw/実 hash の PR 添付は別承認。
- 変更していない snapshot bytes、完了 Producer manifest（raw SHA/byte 長/serialization/ID/版）。
  single JSON か JSONL かは報告・拡張子だけで決めない。圧縮・不完全原本は別審査。
- 対象市場日で有効な正式 universe。`285A`、`2850`、`417A`、`4170` の正式会社名/issuer識別、
  上場期間、版、provider の取得 symbol 対応、別名/改名の根拠。これらの現存・保存を推測しない。
- 実取得 request symbol と応答の対応が分かる証拠、銘柄別 OHLCV 原本（存在する場合）、
  code/provider/symbol/market_date と値の照合証跡。未保存の過去 OHLCV を snapshot から復元しない。
- 完全な銘柄一覧の抽出仕様・辞書・hash。部分標本から対象の不在を断定しない。
- snapshot 観測時刻、銘柄市場日、OVX 基準日をそれぞれ説明する原証拠。公表/取得/計算/依存/
  embargo/改定/暫定の根拠、IANA timezone、calendar 版。generated_at だけでは不足。
- 研究用候補について全探索記録、ADR-002 family/run/重複・再評価の帰属、N_raw 照合証拠。
  未知は null のまま。N=0/1 への補完で審査を進めない。

匿名化・再serialization・行切出しは derived。原本 hash への追跡と変換仕様/code/settings hash を
必要とする。親原本が持ち出せない場合、PR #8 の親実在要件を original 偽称で迂回しない。
提供者側での確認、または承認済み標本専用の契約を別途合意する。

## ① 原本の受入経路と追跡

### serialization による振り分け

Producer の正式完了 manifest の**ファイルごとの宣言**に従う。拡張子・ファイル名・推測で選ばない。
1行 object は両文法を満たし得るため、構文だけでも経路を決めない。宣言不明・矛盾は Hold とする。

| Producer 宣言と原本の条件 | 受入先と個別受入 manifest | 意味・制限 |
|---|---|---|
| 単一 JSON document（object/array） | PR #8 `RawJsonArtifactStore`、`urn:qed:artifact:raw-json:1` | UTF-8・strict JSON 全体を検査。pretty JSON を含め元 bytes をそのまま保存 |
| legacy JSONL、各物理行が strict JSON object、空行なし、末尾 LF あり | PR #5 `LocalAuditStore`、`record_contract=legacy_audit_archive_v1` | 旧原本の保管のみ。record v1 の意味論・研究適格性を確認した扱いにしない |
| JSONL だが上記契約不適合、圧縮、CSV、scalar JSON、宣言不明など | 既存の正常受入経路へ流さず、不適合/未確認として隔離・Hold | 再整形・改行追加・文字コード変換・分割によって原本を「適合済み」にしない |

両経路とも重複 key・NaN/Inf・数値 overflow・不正 UTF-8 を拒否する。PR #8 と PR #5 の store root は
分離する。legacy JSONL の candidates/margin を `audit_received_artifact` に直接渡さない。
PR #9 の同 API は PR #8 の raw JSON store 専用であり、②の証拠照合を代行しない。
JSONL の `prostock2_audit_v1` への変換や新しい regime/candidates 契約の導入は本手順の範囲外。

既定上限は両経路 raw 16 MiB、manifest 4 MiB。legacy は1行256 KiB/10,000行、raw JSON は
depth64/node100,000。容量・inode・reserve と採用 profile を受領前に確認する。上限不適合は別審査し、
元 bytes の変更や無断の上限変更で通さない。既存受信 API が拒否した場合は実際の隔離 receipt を残す。
write/fsync/容量不足で receipt 自体を書けなかった場合は成功とせず、intent/prefix と障害記録を保全する。
上限超過等で保持された prefix は不完全と明記し、完全原本として ACK しない。

### 正式完了 manifest と個別受入 manifest

Producer からパッケージの完了 manifest の**元 bytes**と、ファイル単位の次の項目を正式に受け取る。
実 Producer の field 名・hash 規則・版は正式仕様で対応表を固定し、不明な名前/値を推測で埋めない。

| 正式 Producer 情報 | 記録する内容／不足時 |
|---|---|
| 提供・完了の根拠 | 提供承認、source_system、manifest の版/ID、完了状態、実 export UTC 時刻。未完了は受入成功にしない |
| 各原本の識別 | 安定 file/export ID、serialization/media type、original/derived、source_revision。未知の過去版は未確認 |
| bytes の検証値 | ファイルごとの raw SHA-256・byte 長。完了 manifest 自体も raw SHA-256・byte 長を別記 |
| 時点・対象の説明 | 市場/市場日、各観測版の観測時刻と根拠、universe 版、辞書、行/論理件数の定義と証拠。分からなければ未確認 |
| 派生関係 | derived は親 raw hash と変換 code/spec/settings hash。再serializationした標本を original と呼ばない |

正式 Producer 完了 manifest と receiver が検証する個別受入 manifest は**別の成果物**である。
パッケージに複数原本があれば、各原本について既存契約に適合する個別 manifest を作り、元の
Producer field と値の対応を記録する。元ファイルを連結・分割・再serializationしない。

- PR #8: 契約の全 field を埋める。`artifact_id/export_id` は原本/観測版ごとに安定化し、
  `serialization=single_json_document`、`media_type=application/json`、正確な raw hash/byte 長、
  role/親参照を設定。過去の provenance が未知なら契約上の null と `provenance_gaps` に残す。
  `artifact_kind` は既存 enum に従い、専用分類のない universe 等を勝手に enum へ追加しない。
- PR #5: `transport_schema_version=1`、`source_system=prostock2`、安定 `transfer_id`、
  `record_contract=legacy_audit_archive_v1`、正確な `raw_sha256/size_bytes/record_count`、
  `expected_observations=null`。record_count は契約適合する物理行 object 数であり探索 N ではない。
  Producer の件数定義とも照合する。0・推定値・生存候補数を代入しない。

過去の source ID・版・code/policy を受信側の現在値で補完しない。受信管理用 case/file ID を新規発行する
場合は source の run/revision と区別し、対応表に残す。再送は同じ raw bytes と個別 manifest を使う。

### 根拠と記録先

正式提供時に owner 限定の永続証拠領域を合意する。以下の `<evidence_root>/<case_id>/` は記録先の
役割名であり、まだ実領域を provision/運用接続したとの記録ではない。store の親は事前に永続化する。
追記/別版で訂正し、既存 raw/manifest/receipt を上書きしない。原本や秘密を GitHub/PRへ添付しない。

| 記録先 | 保全するもの |
|---|---|
| `producer/` | 正式完了 manifest 元 bytes、承認・仕様、後述の Producer 照合回答 |
| `stores/raw-json/`, `stores/legacy-jsonl/` | 各既存 API の raw blob・個別 manifest・intent・receipt・隔離記録 |
| `review/intake-index` | 原本ごとに Producer file ID/manifest 内参照、宣言 serialization、経路、raw SHA/byte 長、個別 manifest hash、receipt ID/hash、ACK と実保存先 |
| `review/evidence-register` | ②の資料参照/hash・対象範囲・確認者・確認 UTC・結果・未確認理由・研究への影響 |
| `review/candidates-n-raw`, `review/margin-references`, `review/universe-issuer-periods` | コード外照合の明細・反例・未解決差分 |
| `review/calendar-and-time`, `review/extraction` | 市場日/各版の観測時刻/適用 universe の証拠、辞書/抽出仕様・結果の対応 |
| `facts/`, `reports/`, `acks/`, `review/case-result` | snapshot/観測版別の監査事実・PR #9 出力・durable ACK と試験結果 |

`intake-index` では次の hash を取り違えない。元 raw の hash、Producer 完了 manifest **元 bytes**の hash、
個別受入 manifest の **canonical** hash（receipt の `manifest_sha256`）、receipt の `receipt_sha256`。
後二者は各 API の canonical JSON 規則（sort_keys、compact、ensure_ascii、allow_nan=False）で検証し、
receipt は自身の `receipt_sha256` field を除いた本文の hash と照合する。
これを checksum field を含む receipt ファイル全体の raw hash と同一視しない。配布した manifest/receipt の
元 bytes も保存し、追加の file hash を使うなら名前を区別する。

## ② PR #9 のコード外で照合する証拠

受領前に下表の必要資料・記録先・判定を固定する。現時点で資料確認済みとはしない。
各明細に `確認済み / 未確認 / 矛盾あり / 試験範囲外` を付け、資料 ID/元 hash/該当箇所/対象日・版・run/
確認者/確認 UTC/理由を記録する。「試験範囲外」は成功に含めず、研究に必要なら未確認のまま Hold とする。

| 対象 | コード外で照合する根拠と条件 | 記録先・不足時 |
|---|---|---|
| candidates: 完了 run の網羅性 | 正式 run 開始/完了記録、探索設定、全候補一覧と生成/評価/除外/失敗記録、途中終了の有無を照合。top_k 前の除外、非生存候補、NaN/Inf、棄却が保存範囲から落ちていないか確認。candidates_log 単体や行数で全探索を認定しない | `review/candidates-n-raw`。開始/終了や候補欠落が未解決なら「網羅性未確認」、N は未知のまま |
| candidates: ADR-002 N_raw | run/stage/family/祖先系譜・安定候補識別・batch/台帳証拠を使い全探索と照合。重み/閾値設定変更の帰属、再評価と再送、S1/S2/S3 重複の区別を明記。既存規則は下記の通り維持 | 同明細と `SearchEvidence` の根拠。未知は null、origin=unknown、根拠未提供として研究遮断。ledger_verified の自己申告を追認しない |
| margin | margin データ manifest 本文・Producer 完了 manifest・各参照先原本を取得し、対象市場日・銘柄・source revision・raw SHA/byte 長と対応させる。manifest 自身の受信成功だけで参照閉包や対象の正当性を認定しない | `review/margin-references` に参照1件1行。未提供、hash/日/銘柄/版の不明は未確認、矛盾は別記。参照先の原本も①で個別受入する |
| universe / issuer | 時点別正式マスター、issuer の安定識別、正式会社名、code、provider/request symbol の対応、**銘柄ごとの上場・廃止・code変更・適用期間**を確認。各市場日/観測版に有効な issuer/銘柄を特定し、code 再利用や別名を推測しない | `review/universe-issuer-periods` に issuer/code ごとの期間・根拠。境界の包含規則も正式資料に従う。期間/対応不明なら正式確認 flag を true にせず未確認 |

N_raw は [ADR-002](../adr/ADR-002-lineage-trial-ledger.md) §4.1/4.2/4.7/4.9 に従う。
S1 は batch 内の評価した異なる compiled_expr、stage/run を跨ぐ独立再評価は既存どおり各 batch に計上。
S2 は評価した非 EML 候補の異なる candidate_hash 数で、S1 の EML 生存候補を再計上しない。
family と祖先への帰属を維持し、S3/外部試行との重複は完全一覧と評価証拠で照合する。
同一原本の再送・同一評価の別ID/時刻登録を独立再評価にしない。
全探索 N_raw を JSONL 行数、生存件数、446銘柄、0/1で代用せず、PBO/DSR に自動投入しない。
既存台帳へ今回新規書込や再集計ロジック変更は行わない。

PR #9 の `UniverseEvidence.valid_from/valid_through` は universe 全体の期間であり、
`IdentityEntry` は issuer ID や銘柄別上場期間を持たない。従って上表の確認はコードの期間比較だけでは
完了しない。正式マスターに基づいて各市場日用の入力を組み立て、その選択根拠をコード外に残す。
PR #9 の正式確認/照合 flag は独立レビューの結論を入力するものとし、hash一致や受信成功だけで立てない。

## ③ 正式提供後の実相互試験1件と合否基準

### 開始条件

承認された元 snapshot が**連続2市場日分**と、それぞれを列挙した正式 Producer 完了 manifest が揃ってから
1つの case を開始する。両日の原本/manifest の未提供は **Hold**。受領前に実行済み記録を作らない。
正式辞書、対象市場/calendar 版、時刻根拠、適用 universe/issuer 期間の必須証拠が未確認なら、
①による原本保管だけは可能でも③の総合合格は Hold とする。
同日別観測版が届いていれば case に加え、届いていなければ「未提供・未検証」と記録する。

### 実行・照合表

| 項目 | 実施と合格条件 | 不合格・Hold の区別 |
|---|---|---|
| 原本ごとの経路/保存 | ①の正式宣言と契約に適合する経路で受信し、Producer/receiver/保存 blob の raw SHA・byte 長が一致。UTF-8/非ASCIIを含む元 bytes 不変。個別 manifest/receipt を追跡可能 | hash/byte 長/文法/ID競合は不合格・隔離。宣言や正式 manifest 未確認は Hold。成功 receipt は PROVENANCE_INCOMPLETE |
| 連続2市場日 | 正式辞書が定義する snapshot 対象市場日 D1/D2 について、正式市場/calendar 版で D2 が D1 の次の市場日であると確認し、両原本への根拠を保存。銘柄別 market_date は別に保持し、暦日差・filename・generated_at で判定しない | 日付根拠/calendar不足は Hold。連続しない日付は当該 case の条件不合格。欠けた日を合成しない |
| 各版の観測時刻/適用 universe | snapshot の観測 UTC と根拠を銘柄 market_date/OVX reference_date と分離。両市場日・各観測版の universe hash/版と issuer/code の有効期間を②で確認 | 不足は未確認・Hold、矛盾は不合格。generated_at や受信 UTC で補完しない |
| 正式辞書による抽出 | 辞書/抽出仕様・code commitまたは実内容hash・設定を固定。各監査事実を元 raw hash/該当箇所へ対応させ、抽出件数・完全性・欠損・重複・対象コードの根拠を保存 | 未知 JSON field を推定しない。範囲不明は coverage未確認。NOT_OBSERVED は完全一覧と抽出仕様を確認した入力内に対象完全一致行がない場合のみ |
| PR #9 への入力/判定 | snapshot が単一JSONなら PR #8 receipt と監査事実を `audit_received_artifact` で結合し、出力/入力facts hash/判定理由を保存。`require_received_research_preconditions` の正常戻りまたは ResearchBlocked と根拠を確認 | IDENTITY_MISMATCH・未知N等は入力の問題として保持し、研究を遮断。期待した拒否の確認と入力の適格性を混同しない。JSONL snapshot を同 API に渡す経路は未対応で Hold |
| 同日別観測版（提供時） | 別の安定原本ID/revisionと個別manifest/receiptで保存。各版の観測時刻・universe・抽出/監査結果の差を照合し、旧版を上書きしない | 順序根拠なしは未確認、同一ID異原本は競合。新IDによる別版と再送を混同しない。未提供をこの項目の成功としない |
| 再送/改変確認 | 同じ原本/個別manifest再送は blobを増やさず duplicate_of を追跡し新receiptを検証。承認範囲内で別の負試験copyを1byte改変し、元manifestのまま送って隔離/ACK拒否を確認 | 原本は変更しない。負試験copyをoriginalや実観測版と記録しない。負試験未承認/未実施は未検証で残す |
| Producer 側完了確認 | 下記 hash 照合回答を受領し、raw/正式manifest/個別manifest/receiptの対応を Producer 側でも確認済みと記録 | Receiver側のACK生成だけでは完了しない。回答/根拠不足は Hold、不一致は不合格 |

snapshot 自体が legacy JSONL の場合は①の PR #5 で保管できても、PR #9 raw JSON 専用 API への
無変換の接続はできない。この case を合格にするために JSONL を単一JSONへ再整形せず、
別途レビューした派生/監査接続が必要と記録して Hold とする。

### Producer 側 hash 照合と結果記録

Receiver は `acknowledge(receipt_id)` で原本/個別manifest/receiptを再検証し、durable ACK を保存する。
Producer 担当者は正式提供と合意した確認経路で、各原本の raw SHA/byte 長、Producer完了manifestの
元bytes hash、個別manifest canonical hash、receipt ID/receipt_sha256 と ACK の対応を照合する。
Producer 自身が持つ原本/完了manifestとの一致、照合対象一覧、担当者・確認UTC・結果を含む回答を
`producer/` に保存し、回答自身の元 hash と参照を `review/case-result` に残す。認証情報を記録しない。
未取得の Producer 回答を Receiver が代筆して「確認済み」にしない。

`review/case-result` には、試験scope/使用code版、各原本・市場日・観測版・universe・抽出根拠・
判定/理由・②の未確認事項・各 hash/receipt/ACK/Producer回答参照を記録する。
結果は次を別列で残す。これらは手順上の判定であり、既存 receipt の状態 enum を変更しない。

- **原本受信**: 正常保管は合格、不適合・破損・競合は不合格、前提未提供は Hold。
- **証拠照合/研究preflight**: 確認済み/未確認/矛盾と、PR #9 の BLOCKED 等の実出力を残す。
  未知N・OHLCV不足などの遮断理由を消して合格にしない。
- **実相互試験**: ③の必須項目が完了し Producer 側照合まで一致した範囲のみ合格。
  必須の市場日/観測時刻/適用universe/辞書根拠や回答が不足すれば Hold、矛盾や技術不一致は不合格。
  未提供の任意別版・試験scope外の資料は未検証と明記し、その成功を主張しない。
- **研究利用**: 相互試験が合格でも `research_eligible=false`、受信状態は PROVENANCE_INCOMPLETE。
  candidates の全探索N、margin参照、原OHLCV/PIT等に不足があれば研究Holdを維持する。
  受信合格、期待どおりの研究遮断、研究適格性の承認はそれぞれ別の判断である。

ACK は Producer 原本の削除許可ではない。`producer_deletion_authorized=false` を維持し、
未転送/未確認原本を期限だけで削除しない。正式資料不足を合成fixtureで置き換えて③を完了しない。

## 既存の合成実証記録（今回の実相互試験ではない）

下表は[基準 HEAD の監査記録](../reports/phase8c_identity_acceptance_audit_2026-10-19.md)にある
2026-10-19時点の合成実証を参照する。今回の文書改訂では再実行していない。

| 項目 | 合成試験 | 実 Producer |
|---|---|---|
| raw SHA/byte 長・single JSON・UTF-8/非ASCII保持 | PR #8＋本 Phase のローカル試験 | 未受領・未検証 |
| 原子的保存/fsync・receipt・復旧・改ざん拒否 | PR #8 の既存障害試験を再実行 | 実転送/媒体/電源断は未検証 |
| 再送・1byte改変・別revision・original/derived | 合成 bytes と完了 manifest で確認 | 未検証 |
| 285A/2850、417A/4170 の区別と汚染遮断 | 合成 issuer/universe/取得 symbol の反例 | 実汚染件数・実対象不在は未判定 |
| 未知時刻/OVX日付/原OHLCV/N の遮断 | 明示監査事実で確認 | 根拠未提供・Hold |

合成 fixture に実企業名・実価格・実取得実績は含まない。OHLCV 照合 flag や universe 確認 flag は
テスト前提であり、実原本の照合済みを意味しない。受信はアルファや統計的採用の証明ではない。

## 隔離再現コマンド

本 branch の root で、同じ Python 環境を使い、新規 writable directory に出力する。

```sh
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN <python> -m pytest -q \
  tests/unit/test_identity_pit_audit.py tests/integration/test_identity_artifact_audit.py
<python> scripts/audit/phase8c_identity_pit_probe.py --work-dir <new-local-directory>
```

probe は10の合成シナリオ専用で、実ファイルを受け取る入口ではない。
元 raw、完了 manifest、明示 audit facts、immutable receipt、派生 report を別保存する。
既存 directory は上書きしない。research_eligible は全ケース false。
fixture 用 `America/New_York` は実 OVX source timezone の認定ではない。
DSR/PBO/FROST/Hard Gate/昇格の採用基準は変更しない。
