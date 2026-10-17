# ProStock2 → Q.E.D 監査データ契約 v1

本仕様は将来ProStock2が出力する監査レコードをQ.E.Dが検査するための提案契約である。既存EC2にQ.E.D・PostgreSQL・EML/FROST実行環境が存在するとは扱わない。実運用DB、親スキーマ、実履歴の取得先は未確定であり、生産者との合意・実データ照合は未実施である。

単一レコードの機械可読仕様は [JSON Schema](../../qedschema/contracts/prostock2_audit_v1.schema.json)（Draft 2020-12）。Schemaの構造検証に加え、後述の純関数による型・時刻・hash・レコード間の意味論検証を必須とする。Schemaの`format`を注釈として扱う実装もあるため、Schema検証だけで日時が正しいと判断しない。

今回の範囲はTask A/Bの契約・純関数検証である。Task Cの転送ファイル検証、原子的な永続化、再送冪等性、隔離、監査receipt、Task Dの新たな全探索保存処理は実装しない。既存FROST DTO・DSR/PBO・選抜・昇格・ProStock2 cron・売買管理への自動接続はない。受入適格性と統計的アルファの採用判断は独立であり、NOTE-008の採用評価は実データ取得までHoldとする。

## 1. レコード境界と必須項目

wire形式はUTF-8の単一JSON objectで、全定義フィールドを必須とし、未知フィールドを拒否する。JSONの重複キー、非有限数、booleanの整数代用、数値の暗黙変換、naive日時、時差付きUTC以外の日時表現を拒否する。JSON Schemaの数学的なinteger解釈だけに依存せず、純関数は厳密な型を検査する。

| フィールド | 意味・形式 |
|---|---|
| `schema_version` | integerの`1`。`true`や`1.0`で代用しない |
| `run_id`, `record_id` | 生産者側の安定した実行・証拠識別子 |
| `source_system` | 定数`prostock2` |
| `record_type` | `ohlcv` / `feature` / `us_macro` |
| `symbol` | 対象銘柄または生産者が定義するマクロ対象識別子。先頭英数字のASCII識別子、最大64文字 |
| `data_date` | 実在する`YYYY-MM-DD`。OHLCV/特徴量は対象データ日、マクロは参照期間日。時刻や日本の利用可能営業日の代用ではない |
| `universe_version` | 当時の銘柄集合・対象集合の固定版 |
| `measurement_id` | payload欠損時にも保持する計測対象識別子。OHLCVは`ohlcv`、特徴量は`payload.name`、マクロは`payload.series_id`と一致 |
| `event_time` | OHLCVはバー終了時刻、マクロは当該vintageの実公表時刻、特徴量は生産者が定義する評価対象イベント時刻 |
| `captured_at` | 生産者がこの証拠レコードを取得・固定した時刻。常に非null |
| `available_at` | 必要な公開・受信・計算・embargo条件を満たし、この版を利用可能とする最早の保守的な時刻 |
| `data_cutoff` | このレコードを利用する評価の情報締切時刻。対象データ日の代用にしない |
| `source_revision` | 原ソースの版・vintage識別子。初版も生産者の追跡可能な識別子を明示 |
| `code_commit` | 生産者コードの40または64文字の小文字hexコミットID |
| `policy_hash` | `sha256:`に続く64文字の小文字hex。生産者のexport／上流gateに用いた固定policy本文を識別する。Q.E.Dの現在policyを代入せず、既存FROSTポリシーも変更しない |
| `market_data_hash` | 原市場データbytesのSHA-256宣言（bare 64文字小文字hex）。原本との照合はTask C以降 |
| `payload_sha256` | §5の規則による`payload`のSHA-256（bare 64文字小文字hex） |
| `upstream_gate_state` | `passed` / `failed` / `not_evaluated` / `unknown` |
| `status` | `ready` / `missing` / `rejected` |
| `reason_codes` | 重複のないASCII理由コード配列。理由の自由文に秘密情報を埋め込まない |
| `missing_fields` | nullになったnullable rootフィールド名の集合を過不足なく列挙 |
| `availability` | 公開・受信・計算・embargoと依存証拠の構造。§3参照 |
| `payload` | record_type別object、または理由付きnull。§4参照 |

`run_id`、`record_id`、`universe_version`、`measurement_id`、`source_revision`、理由コード等の識別子は`[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}`とする。空文字や空白を認めない。実在する元の識別子を受け入れるための適合ルールであり、未知のIDを生成して事実の代わりにしない。`measurement_id`は常に非nullで、payloadが欠けてもどの特徴量・系列が欠損したかを失わない。

nullable rootフィールドは`event_time`、`available_at`、`data_cutoff`、`source_revision`、`code_commit`、`policy_hash`、`market_data_hash`、`payload_sha256`、`payload`の9項目である。unknownをゼロ・推測時刻・仮のhashで埋めない。

| status | 必須条件 |
|---|---|
| `ready` | nullable rootがすべて非null、上流gateは`passed`、理由・欠損配列は空、型別の証拠と時刻条件が全て成立 |
| `missing` | 理由が1件以上、nullable rootが1件以上null、`missing_fields`がその集合と完全一致 |
| `rejected` | 理由が1件以上、nullがあれば`missing_fields`に正確に列挙。欠損がなくても拒否理由を表現可能 |

`missing`/`rejected`も識別子、`captured_at`、`data_date`、`universe_version`、`measurement_id`を省略しない。これらさえ取得できない壊れた入力は有効な監査レコードではなく、将来のTask Cで受信失敗として扱う対象である。既知の不正な日時・型・hashを`rejected`に変えるだけで検証通過させない。欠けた証拠はnullと理由で表現する。statusによらずpayloadが既知なら正しい`payload_sha256`を必須とし、payloadがnullなら`payload_sha256`もnullにする。

## 2. UTCと時間順序

すべてのinstantは`YYYY-MM-DDTHH:mm:ss[.ffffff]Z`とする。小数秒は任意の1〜6桁、UTCの大文字`Z`のみを受け付ける。naive、`+09:00`、`-05:00`、`+00:00`、存在しない日付、うるう秒等を受入時に推測変換しない。生産者が根拠を確認した上でUTCへ変換する。公表元の`release_local_date` / `release_local_time`は元の現地表記ラベルであり、保存されるUTC instantの例外ではない。

値が存在する箇所について、少なくとも次の順序を検証する。

```text
event_time <= available_at
raw OHLCV / macro: published_at <= received_at <= captured_at
feature: received_at <= computed_at <= published_at <= captured_at
feature: computed_at >= all dependency.available_at
all observed instants, including bar_start and dependency.available_at <= captured_at
available_at >= max(published_at, received_at, computed_at,
                    embargo_until, all dependency.available_at)
available_at <= captured_at
ready: available_at <= data_cutoff <= captured_at
```

maxの比較では該当しないnull項目を除くが、`ready`の公開・受信時刻は必須である。観測済み時刻はstatusによらず`captured_at`以下とする。`embargo_until`は予定時刻のため、`available_at`がnullの非readyレコードでは未来の値を保持できる。`embargo_until: null`は生産者が「embargoなし」と宣言した意味とし、「不明」を表さない。embargo有無が不明なら`ready`にしてはならない。契約は生産者の不正な過去時刻申告を検知・証明できるものではない。

分析のas-of/decision cutoffより後に`available_at`を持つ値を利用しない。`recorded_at`やDBコミット可視性は本契約の公開・受信時刻とは別であり、混同しない。後日backfillしたデータの`captured_at`を過去に付け替えず、当時の受信証拠がない場合は過去のPoint-in-Time適格性を保証しない。

## 3. 利用可能時刻と依存証拠

`availability`は全フィールド必須のobjectである。

```text
published_at: UTC Z | null
received_at: UTC Z | null
computed_at: UTC Z | null
embargo_until: UTC Z | null
dependencies: [
  {source_system: "prostock2", run_id, record_id,
   evidence_sha256: bare SHA-256, available_at: UTC Z}
]
```

OHLCV/マクロの`published_at`はその版の原ソース公開時刻、`received_at`は生産者がその版を実際に受信した時刻である。特徴量の`received_at`は計算に必要な入力の最終受信時刻、`computed_at`は計算完了時刻、`published_at`は計算した特徴量自体の実公開時刻である。特徴量では入力受信→計算→特徴量公開の順序とし、原データの公開→受信と区別する。

`ready`で`published_at`と`received_at`は非null。`feature`の`ready`は`computed_at`と1件以上の依存証拠を必要とする。OHLCVとマクロは`computed_at: null`、依存配列を空とする。依存情報が欠けた特徴量は`ready`ではない。

### OHLCV

`payload.bar_start < event_time <= published_at`を満たし、`event_time`を当該バーの終了時刻とする。日次OHLCV全値はその日の終値・最終volumeが確定する前には利用できない。バー終了直後に無条件で利用可能とせず、配信受信・確定処理・訂正の遅延を`available_at`へ反映する。休場日・短縮取引日・取引時間変更は`calendar_version`に紐づく正規カレンダーの責任範囲で、純関数は独自カレンダーを作らない。

### 特徴量

computed_atが不明な非readyでも、既知のreceived_atと依存available_atがpublished_atを越える因果矛盾は拒否する。すべての必要入力を`dependencies`で固定し、計算完了は全入力の受信と利用可能時刻以降、特徴量公開は計算完了以降とする。利用可能時刻は全入力・計算完了・特徴量公開より前にしない。batch検証は`(source_system, run_id, record_id)`で依存先をbatch内に解決し、そのレコードの完全証拠hashと`available_at`の一致を検査する。未解決依存・循環を拒否する。外部の未受入データへ暗黙にfallbackしない。生産者が必要な入力自体を申告しない場合、その完全性をhashだけから証明することはできない。

### 米国マクロと日本市場

`data_date`は参照対象の経済期間の日付であり、公表日・日本の営業日・利用可能日と同一視しない。`event_time = published_at`とし、`source_revision`で初版と改定vintageを区別する。現在取得した改定値を過去の初版公開時刻へ遡及させない。

`release_timezone`は`America/New_York`等、`/`を含むIANA locationを必須とし、`EST`等の裸の略称や`Etc/`の固定時差zoneを拒否する。実行環境のtimezone databaseも版固定する。`published_at`を`ZoneInfo(release_timezone)`へ変換した現地日付・時分秒が、`release_local_date`と`release_local_time`に一致することを検証する。たとえば`America/New_York`の08:30は通常、冬時間には13:30Z、夏時間には12:30Zとなる。固定EST変換・存在しない現地時刻・DST誤変換を拒否する。曖昧な現地時刻については宣言したUTC instantが版の根拠であり、元データだけから推測しない。実公表時刻が未取得なら日付から補完しない。

最初に利用できる日本市場の判定時刻は、呼出側が版固定した正規カレンダー由来のdecision UTC時刻列から、`available_at`以降の最初の時刻を選ぶ。`first_eligible_decision`は休日や取引セッションを計算せず、時刻列が不足する場合は`None`を返す。「米国公表の翌日」「翌営業日9時」へ固定変換しない。同時刻なら利用可能とする境界規則（`>=`）を使い、運用の受信・計算latencyは`available_at`へ保守的に含める。カレンダー版を明示しても、その内容の正しさは別途照合が必要である。

## 4. payloadの厳格型

各objectは以下の全フィールド必須で、追加フィールドを拒否する。

| record_type | payload |
|---|---|
| `ohlcv` | `open`, `high`, `low`, `close`: decimal文字列、`volume`: 0以上のsafe integer、`bar_start`: UTC Z、`calendar_version`: 識別子 |
| `feature` | `name`: 識別子、`value`: decimal文字列、`unit`: 識別子 |
| `us_macro` | `series_id`: 識別子、`value`: decimal文字列、`unit`: 識別子、`release_timezone`: 実在するZoneInfo名、`release_local_date`: 日付、`release_local_time`: `HH:mm:ss[.ffffff]` |

decimal文字列は`^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`、最大64文字とする。指数、binary float、NaN/Infinity、先頭`+`、先頭ゼロを認めず、文字列の精度をそのまま保持する。`"1"`と`"1.0"`は異なる証拠である。OHLCV価格は正、`low <= open <= high`かつ`low <= close <= high`を検査する。特徴量・マクロには一律の正値制約を設けない。

integerは`0..9007199254740991`の安全な範囲を使う（現仕様のvolume）。booleanや小数表記での代用を拒否する。数値の単位・調整方法・特徴量定義・マクロ系列とsymbolの対応は生産者側データ辞書の合意が必要で、この仕様が捏造するものではない。

## 5. hashの境界

canonical bytesを次のPython標準JSONの出力のUTF-8 bytesと定義する。

```python
json.dumps(value, sort_keys=True, ensure_ascii=True,
           separators=(",", ":"), allow_nan=False).encode("utf-8")
```

これはRFC 8785/JCS互換を標榜する規則ではない。他言語の生産者はフィールド順・ASCII escape・区切り・strict integer・decimal文字列を含む同一規則を実装して共通fixtureで照合する。受信後にdecimalの桁や文字列を正規化しない。

```text
payload_sha256(payload) = SHA256(canonical_bytes(payload))
evidence_sha256(record) = SHA256(canonical_bytes(record))
```

`payload_sha256`はpayloadだけをhashし、自己参照はない。証拠hashは`payload_sha256`を含むレコード全体をhashするため、ID・時刻・status・availability・依存宣言の変更も異なる証拠となる。依存先の`evidence_sha256`にはこの完全レコードhashを使う。

ファイルのraw-byte SHA-256はこれらとは別物であり、改行・空白等も対象となる。ファイルhash検証はTask Cの範囲で、今回のdecoderはファイル署名・転送完了・永続化を保証しない。`market_data_hash`は原データbytesのhash宣言であり、本validatorは未取得の原本を照合しない。どのhashも原証拠の完全性・真正性・公表時刻の真実性・過去に利用可能だったことの証明ではない。

## 6. 純関数の受入APIとbatch検証

```text
decode_record(raw: bytes) -> dict
validate_record(document) -> dict
payload_sha256(payload) -> str
evidence_sha256(record) -> str
validate_records(records, expected_observations=None) -> tuple[dict, ...]
first_eligible_decision(record, decision_times, *, calendar_version) -> UTC str | None
```

これらはDB・ネットワーク・現在時刻に依存せず、受信ファイルの読み書きを行わず、入力証拠と明示パラメータを検査する。IANA変換は版固定したローカルtimezone databaseを参照するため、その版は再現性の依存条件である。decodeはJSONの重複キー等も拒否する。`validate_record`は独立したcopyを返し、厳格型・時刻・欠損集合・payload hashを検査するが、単一レコードだけでは依存先や期待coverageを証明できない。validation errorは固定のcode/pathだけとし、入力値・生JSON・認証情報を出力しない。

`validate_records`は各レコードの検証に加え、次を検査する。

1. `(source_system, run_id, record_id)`の重複を拒否する。同一ID・異なる完全証拠hashは競合である。同じ証拠の再送を永続受入で冪等に扱う仕組みはTask Cに分離する。
2. 自然キー`(source_system, run_id, record_type, symbol, data_date, universe_version, measurement_id, source_revision)`の重複を拒否する。payloadがnullでも`measurement_id`があるため、別の欠損特徴量を同じ観測へ潰さない。`source_revision: null`同士で他の自然キー項目も同じ場合は同一の不明版として重複を拒否する。改定vintageや独立runを同一証拠へ潰さない。
3. 特徴量の全依存先がbatch内に存在し、依存先が`ready`で、完全証拠hash・利用可能時刻が一致し、依存関係に循環がないことを検証する。
4. `expected_observations`はsetまたはfrozensetで指定し、listの暗黙的な重複除去はしない。指定された場合、`(source_system, run_id, record_type, symbol, data_date, universe_version, measurement_id)`の7-tuple集合と完全一致を要求し、欠落日付・銘柄・特徴量と想定外の観測を検出する。未指定の場合、受信分だけから完全性を宣言しない。これは観測単位の存在確認であり、statusが`missing`なら値が揃った意味ではない。期待集合の信頼性・本来の全銘柄集合・取引日カレンダーとの一致は未実装のmanifest監査に残る。

batch閉包は現段階の保守的な制約である。既存の永続証拠を参照するresolverは今回追加しない。alias ID・新規run・source_revisionの虚偽による二重申告を入力だけから完全に検知することはできず、生産者の安定ID・全出力・原証拠の監査を別途要する。

## 7. 今回保証しない事項と取得待ち情報

synthetic fixtureで保証するのはソフトウェア境界の品質であり、統計的エッジ・DSR適格性・実売買採用ではない。過去の探索Nを1や0へ補完せず、ADR-002のN_raw・再評価・派生帰属を変更しない。

実結合の開始には、生産者の正式スキーマ／データ辞書、安定したID・vintage規則、実際の公表・受信・計算証拠、universeと取引カレンダー版、原データとそのhash、全候補・拒否・失敗を含む出力、全探索N、コスト後OOS returns、転送先と権限設計が必要である。親`evidence_bundles` / `factor_candidates`の実スキーマは未取得で、既存の外部依存7件は引き続きHold。Codex CloudのPostgreSQLテストDBと合成fixtureを本番履歴として使わない。

Task Cで検討する永続保存・再試行・隔離・receiptは、生産者cronと売買系から分離する。今回の純関数は生産者や本番DBを書き換えず、失敗時も未知値を生成して後続処理へ流さない。認証情報を契約payload・理由コード・PRの例に含めず、未取得の実データ内容を推測して掲載しない。
