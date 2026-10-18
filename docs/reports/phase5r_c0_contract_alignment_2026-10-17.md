# Phase 5R C-0 — ProStock2 生産者と Q.E.D 受信契約の照合

調査日: 2026-10-17。契約基準は Draft PR #4 の
`c92cef13ed01542145c2e97989efce88f9bd64e5` にある
[ProStock2 audit v1](../contracts/prostock2_audit_v1.md)、
[JSON Schema](../../qedschema/contracts/prostock2_audit_v1.schema.json)、
[純関数 validator](../../analytics/python/data_contracts/prostock2_v1.py)。
本監査は利用者が提示した所在監査結果とソース上の契約との照合である。
EC2、本番 DB、cron、実売買には接続せず、実ファイルの内容・件数・hash・時刻を直接検証していない。

## 1. 提供された事実と未確認の範囲

| 利用者が提示した所在監査結果 | 現時点で判断できること | 原本で確認する事項 |
|---|---|---|
| 過去 91 件の銘柄別 snapshot に 446 銘柄の特徴量が存在 | 特徴量の過去原本候補がある | 91 件が表す単位、銘柄集合、対象日、行数、重複、欠損、型、計算定義、原本 bytes |
| 日足 OHLCV 原本は未保存 | 既存の特徴量から OHLCV 原本・hash を復元したとは扱えない | 将来の原本保存先、vendor/vintage、調整方法、取引 calendar、公開・受信・確定時刻 |
| OVX・JVRAM・一部 regime 履歴が存在 | 個別系列・状態の原本候補がある | 系列の意味・単位・頻度・地域、数値かカテゴリか、改定履歴、公表・計算・利用可能時刻 |
| `candidates_log.jsonl` が存在 | 候補ログの原本候補がある | 全候補性、記録開始時点、候補 ID、run、設定、棄却・失敗・中断、全探索 N、選抜前後の欠落 |
| 約定・コスト・損益履歴は未取得 | コスト後 OOS returns や実運用損益の検証はできない | 期間・約定・費用・slippage・ポジション・returns 定義と証拠 |
| Git commit・policy 履歴は未取得 | 過去のコード／policy を現 HEAD／現在 policy で埋められない | 当時の固定版、設定本文と hash、実行との対応 |
| 過去 `available_at` は一部だけ根拠を確認可能 | 全期間の Point-in-Time 適格性は宣言できない | 根拠のある対象・時刻・タイムゾーン・vintage、根拠なしの対象範囲 |

「存在」は受信済み・真正性確認済み・完全性確認済み・研究適格と同義ではない。
91 と 446 を掛けた値を観測数・評価試行数・探索 N として計上しない。
存在が報告されたファイルの再取得、実 EC2 ファイルによるテスト、提供先への接続は、正式な提供と承認を待つ。

## 2. v1 必須項目ごとの変換可否

v1 の `record_type` は `ohlcv` / `feature` / `us_macro` のみである。
以下の「未確認」は値が必ず欠けるとの断定ではなく、今回の提示情報だけでは実在値を確定できない意味である。

| 項目 | 所在監査との照合・判定 | 許される表現／停止条件 |
|---|---|---|
| `schema_version` | 既存原本が v1 である証拠はない | 将来の正当な変換物に integer `1` を付ける。原本自体が v1 だったと書き換えない |
| `run_id`, `record_id` | 原本内の安定 ID と実行境界は未確認 | 正式な exporter の識別規則を合意するまで既存評価 ID を推測しない。v1 は null 不可 |
| `source_system` | 生産者は ProStock2 と指定 | 正式 export の `prostock2`。受信側が不明な第三者データへ一律付与しない |
| `record_type` | 数値特徴量は `feature` の候補。他は §3 | ファイル名だけで OHLCV／マクロ／特徴量と断定しない。候補ログの一括 feature 化は禁止 |
| `symbol` | 446 銘柄の報告はあるが個別識別子・履歴未確認 | 原本とデータ辞書で確定。総銘柄数から補完不可。v1 は null 不可 |
| `data_date` | 各 snapshot の対象データ日は未確認 | 原本で対象日を確認。ファイル取得日・mtime・公表日との同一視は禁止。v1 は null 不可 |
| `universe_version` | 当時の集合・版は未取得 | 446 という件数だけから版を作らない。v1 は null 不可 |
| `measurement_id` | 個別特徴量名・系列名の対応は未取得 | データ辞書で一意に固定。v1 は null 不可 |
| `event_time` | 評価対象イベント時刻／実公表時刻の根拠は対象ごとに未確認 | 不明なら nullable root の null と正確な `missing_fields`。日付から時刻を補わない |
| `captured_at` | 当時の producer 証拠固定時刻は未確認 | v1 は null 不可。今回の Q.E.D 受信時刻・mtime を過去の producer capture 時刻として代入しない |
| `available_at` | 一部のみ根拠確認可能 | 対象ごとに根拠を保持。根拠なしは null。全件一律で市場終了時刻や翌営業日に設定しない |
| `data_cutoff` | 過去評価の情報締切は未確認 | 評価の固定証拠がなければ null。`data_date` や受信時刻による代用不可 |
| `source_revision` | 原版・訂正版・vintage の識別規則は未確認 | 不明なら null。現在版を過去初版へ遡及しない |
| `code_commit` | 過去履歴未取得 | null。Q.E.D／ProStock2 の現在 HEAD、ゼロ hash、推測コミットの代入禁止 |
| `policy_hash` | 過去履歴・policy 本文未取得 | null。現在 policy の hash や仮値による代用禁止 |
| `market_data_hash` | 日足 OHLCV 原本未保存。その他原本 bytes も今回未提供 | 実在する対応原本の照合なしに生成しない。snapshot 自身の hash を原市場データ hash と呼ばない。不明なら null |
| `payload_sha256` | 将来の正当な変換 payload に対しては算出可能 | v1 canonical payload hash。原ファイル raw-byte hash と別。payload null なら hash も null |
| `upstream_gate_state` | 各記録当時の実判定状態は未確認 | 証拠なしは `unknown`。数値があるだけで `passed` にしない |
| `status`, `reason_codes`, `missing_fields` | 保存の成否と別のレコード内状態 | 証拠が不足するなら `missing`／`rejected` と理由・正確な null 集合。既知の不正値は status 変更で救済しない |
| `availability.published_at`, `received_at`, `computed_at` | 特徴量公開・入力受信・計算完了の当時の根拠は未確認 | 不明は null。現在の転送時刻で過去の順序を作らない。ready feature は全てと依存閉包が必要 |
| `availability.embargo_until` | embargo の有無も対象ごとに未確認 | null は「なし」の宣言であり「不明」の代用ではない。不明なら ready／研究適格にしない |
| `availability.dependencies` | OHLCV 未保存により少なくとも一部入力証拠は解決不能 | 欠落依存を仮 hash・空配列で ready にしない。ready feature は同一検証 batch 内で依存先を解決し hash・available_at・ready を照合する |
| `payload` | 特徴量の実数値・単位・原精度は未提供 | 値のあることと利用可能性は別。未知は null／理由。NaN、Inf、boolean、カテゴリを decimal へ無断変換しない |

v1 は欠落を一部表現できるが、`missing`／`rejected` でも
`run_id`、`record_id`、`symbol`、`data_date`、`universe_version`、`measurement_id`、`captured_at`
などの非 nullable 項目は必須である。したがって過去の原本を必ず v1 missing record へ変換できるわけではない。
項目が不明なら transport archive で原本を保持し、v1 の検証成功を装わない。

## 3. regime と candidates の契約境界

### 数値指標・regime

OVX、JVRAM という名称だけでは `us_macro` と判定できない。
`us_macro` は実公表時刻、IANA 現地時刻、系列・単位、vintage など v1 のマクロ契約を満たす場合に限る。
OVX が特定の指数系列、JVRAM が独自計算指標だという推定も今回の変換根拠にしない。

regime 関連の測定が、既存 v1 feature として正当に定義された数値指標であり、
測定名・decimal 値・単位・入力定義・コード／policy 版・依存証拠・実際の公開／受信／計算／利用可能時刻を
裏付けられるなら `feature` へ表現できる。上流分類を数値化する場合でも、その測定定義が既存契約と
整合することが先であり、今回新しくラベルへ整数を割り当てて数値 feature と呼ばない。

カテゴリ状態、状態遷移、判定確率ベクトル、複数状態の根拠、モデル版と分類判定履歴は v1 の
`{name, value, unit}` へ損失なく収まる保証がない。独立 `regime` レコードや後続版を提案するが、
今回 v1 の enum／payload／意味を変えない。原本を archive し、正式データ辞書・実標本と照合後に
新しい schema ID と contract version、専用 validator、移行規則を別レビューする。

### 候補ログ

`candidates_log.jsonl` は市場の単一数値特徴量とは異なる。
候補 ID、生成・評価・棄却・失敗・選抜のイベント、run／family／policy／code、スコアと gate、
全探索範囲を必要とするため、候補全体を feature の値や単位文字列へ埋め込まない。
独立した候補／探索イベント契約は Task D の監査・将来設計と分離する。

候補ログが存在しても全探索を網羅した証拠ではない。top_k 前の除外、NaN／Inf、未保存、
クラッシュ、設定変更、再評価、派生の欠落を確認するまで ADR-002 の N_raw と結び付けない。
既存 S1/S2/S3 への自動加算・二重計上を行わず、過去 N は unknown のままにする。

## 4. v1 を変えずに原本を受け付ける境界

Task C では record contract と transport manifest の版を分離する。
過去原本の保管用 `record_contract = legacy_audit_archive_v1` は、
原本 bytes と提出 manifest／受信 receipt を識別する transport 側の契約名であり、
新たな研究レコード v1、FROST 候補契約、統計的適格性の意味を与えない。
この archive 経路で v1 の必須項目を欠いた raw JSONL を保持しても、v1 record validator の成功とは記録しない。
破損・strict JSON 違反・転送 hash 不一致は archive でも隔離対象とする。
過去 snapshot の実際のファイル形式は未確認である。今回の受信境界は strict JSONL であり、
複数行の単一 JSON、CSV、圧縮ファイル等を自動で変形・展開して原本と呼ばない。
形式が合わない原本は隔離された bytes として保全し、正常な archive 契約検証とは区別する。
必要なら別の raw artifact transport を実標本と照合して定義する。

将来の正式 v1 export は既存 `schema_version: 1` の意味と validator をそのまま使用する。
受信側が勝手に過去ファイルへ必要項目を追記して ready に変換しない。
互換性を壊す新 record type や必須項目の変更は、別 schema ID／版と生産者との合意を要する。

受信側の actual `received_at` は現在の受信事実として receipt に記録できる。
これは原本中の過去 producer `captured_at`、`available_at`、コミット可視性時刻とは別である。
raw-byte hash は改行・空白も含めて固定するが、原証拠の真正性・網羅性・当時の利用可能性を証明しない。

## 5. 受信状態から研究適格性への昇格を防ぐ

取込処理の `RECEIVED`、`INTEGRITY_VERIFIED`、`CONTRACT_VALIDATED`、
`PROVENANCE_INCOMPLETE`、`RESEARCH_ELIGIBLE`、`QUARANTINED` は、
レコード内の `ready`／`missing`／`rejected` とは別の状態である。

| 確認結果 | 保証できること | 保証しないこと |
|---|---|---|
| 原本を原子的に保存 | その受信 bytes の再取得 | 過去の観測時刻、全期間網羅、研究利用許可 |
| raw-byte hash 一致 | 提出宣言と保存原本の一致 | 生産者の真実性・完全性、原市場データの一致 |
| strict JSON／v1 検証成功 | 型・時刻順序・宣言した依存の整合 | 申告から欠けた依存、虚偽時刻、対象集合自体の完全性 |
| coverage 集合一致 | 提出された期待集合との一致 | 全銘柄・全日付・全探索が期待集合に含まれること |
| provenance の不足なしとの提出申告 | 提出者の主張を保存 | 受信側の独立検証、研究許可、FROST 昇格 |

manifest、入力レコード、ファイル名に `RESEARCH_ELIGIBLE` と書くだけで昇格できる API は設けない。
既存レコード status が ready でも、受信実装が真実性を確認できないため自動研究許可を付けない。
archive は `PROVENANCE_INCOMPLETE` までとし、研究用 resolver／FROST／DSR／昇格へ渡さない。
将来の研究許可は、必要データ・時点整合性・原本・依存完全性・独立した検証条件を別の
版管理された手順で証明してから行う。今回その研究許可経路は実装しない。

上書き禁止・重複受信の冪等性も、研究 N の自動計上とは独立である。
別 transfer ID や再送によって新規試行を作らず、試行の独立再評価・派生は ADR-002 の規則で別途判定する。

## 6. Exporter との残る差分と Go / Hold

正式な生産者実装・実標本は未提供であるため、双方の適合性を実証したとは報告しない。
Exporter と合意する残件は次のとおり。

1. raw 原本の提供範囲、安定 run／record／transfer ID、変更版と再送の識別、ファイル境界。
2. 原本の元名・bytes と hash、完了 manifest、件数・最大長・期待日付／銘柄／測定集合の生成根拠。
3. producer capture と receiver receipt の別時刻、過去 availability の根拠がある範囲、欠落範囲。
4. 当時の universe／calendar、code／policy、source revision、入力原本と依存証拠の提供方法。
5. OVX／JVRAM／regime の正式データ辞書、候補ログの契約、全候補性と全探索 N。
6. 転送・隔離・再試行・容量不足時の運用、保存確認 receipt の照合、期限だけで未転送原本を削除しない手順。
7. 公式に提供・承認された匿名化標本での相互検証。認証情報や実データ本文を PR／ログに含めない。

**Go** は、ProStock2 と独立したローカル受信・原本保管・隔離・receipt のソフトウェア品質検証まで。
合成 fixture はこの品質検証に限定し、アルファや実履歴の Point-in-Time 適格性を証明しない。
**Hold** は、EC2 実ファイル照合、親 DB 実結合 7 件、過去 v1 変換の承認、研究資格付与、
NOTE-008 採用評価、全探索 N の確定、コスト後 OOS 検証。
DSR・PBO・既存 FROST ポリシー・Hard Gate・昇格・本番 DB・main は変更しない。
