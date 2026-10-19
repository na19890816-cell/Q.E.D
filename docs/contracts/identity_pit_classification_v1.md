# Phase 8C — 銘柄同一性・PIT・研究不適格の分類 v1

2026-10-19。内部監査契約 `urn:qed:audit:identity-pit:1`。
PR #8 `2d2d5dc1` を base とし、既存 record v1、strict JSONL transport v1、raw JSON artifact v1 は
変更しない。実 ProStock2 bytes・正式 universe・取得元 symbol 証拠・辞書は **未提供**。
本書の監査入力はレビュー済みの明示的事実であり、実 snapshot の未知の JSON field を捏造する
新 Exporter schema や自動変換器ではない。

## 文字列・銘柄の同一性

`285A != 2850`、`417A != 4170`。英字除去、0置換、数字抽出、整数化、case-fold、NFKC、
trim、provider suffix の除去によってコードを同一視しない。
`code_mutation_signals` は破壊的変換の兆候を返すだけで、修正コードを返さない。
英字除去と数字のみ抽出は同じ出力になり得るため、どちらを実行したか断定しない。
ここでの4桁 ASCII 英数字という入力制限は公式 JPX 番号規則の代替ではなく、正当性は
正式に提供された時点別 universe に依存する。コード・会社名・symbol の自動修復はしない。

UniverseEvidence は版、raw hash、適用期間、code/正式会社名/provider/実取得 symbol の対応、
formal 提供確認・symbol mapping 確認、market timezone/calendar 版を保持する。
確認 flag は既定 false。hash や caller の宣言だけで発行元の真正性を証明しない。
会社名は完全一致で確認し、略称・改名・provider 別名の未知対応を推測しない。
正当な別名や上場変更は正式な時点別対応証拠を取得して再審査する。
一つの正式 code または provider/symbol が複数 issuer に衝突する universe は例外で拒否する。

| 同一性 status | 意味／研究への扱い |
|---|---|
| IDENTITY_MISMATCH | 正式対応と会社名/取得 symbol が矛盾、別正式 code に衝突、または OHLCV の銘柄/日付が矛盾。遮断 |
| NOT_OBSERVED | 完全な観測一覧と抽出仕様を確認した入力内に、対象 code の完全一致行がない。欠落として遮断 |
| OBSERVATION_COVERAGE_UNVERIFIED | 一覧が未確認・一部しかないため対象不在を証明できない。遮断 |
| IDENTITY_UNVERIFIED | 正式 universe/取得 symbol/時点/会社名等が未確認。正常と推定せず遮断 |
| CONSISTENT_WITH_SUPPLIED_EVIDENCE | 明示入力間で矛盾なし。実証・研究許可を意味しない |

対象判定は保存された code による完全一致で行う。`2850` の行に `285A` の会社名が付いていても、
`285A` の観測として数えない。`2850` 行は別途不一致として保存し、`285A` は範囲確認時にのみ
NOT_OBSERVED。実原本が未提供の現在は、実対象の status は **NOT_ASSESSED_NO_ARTIFACT** である。
未受領を NOT_OBSERVED と報告しない。

## 明示的な監査入力

実装は frozen dataclass と tuple を受け取る純関数。入力を変更・補完せず、結果に全入力の
canonical SHA-256 を固定する。文字列は非ASCIIを維持し、型・UTC・実在日付・hash を検査する。
例外は安全な固定 code のみ。実データの code/ID/hash の公開にも正式な許可が必要。

| 入力 | 必要事実と不足時の扱い |
|---|---|
| SnapshotEvidence | raw SHA、snapshot 観測 UTC、その証拠 hash、generated_at（別項目）、完全一覧確認、抽出仕様 hash。時刻や一覧不明は遮断 |
| Observation | record ID、保存 code、会社名、実 provider/symbol、銘柄の market_date。値を他銘柄へ付け替えない |
| OhlcvMatch | 原 OHLCV hash・照合証跡 hash、code/provider/symbol/market_date と一致確認。未知・未照合・不一致は遮断 |
| PitValue | 系列名・基準日・基準日証拠・source timezone・available_at・利用可能性証拠・根拠種別・暫定/改定状態。不明は strict PIT から除外 |
| SearchEvidence | N_raw（未知 null）・ledger_verified/unknown/imputed・照合証拠 hash。未知/推測補完/証拠不足は遮断 |
| target_codes / required_series | 原本から独立に固定した対象・必要系列。監査範囲に含めなかった対象の完全性を主張しない |
| decision_at | 研究で使おうとする意思決定 UTC。未来からの入力を認めない |

OhlcvMatch.matched、正式確認 flag、history_verified は**独立レビューによる入力事実**であり、
この純関数が実 OHLCV や当局の原証拠を取得・照合したとの宣言ではない。
正式辞書・原本がなく flag を true にしてはならない。単なる price 値の一致、hash、receipt、
synthetic fixture は真正性・完全性・当時の利用可能性を証明しない。

## PIT の分離と除外

1. snapshot 観測時刻、生成時刻、各銘柄の market_date、OVX 等の reference_date は別項目。
   generated_at / filename / mtime / receiver 時刻を historical available_at にしない。
2. 日時は timezone を必須とし、この内部契約の入力は UTC `Z`、小数秒最大6桁。
   非UTCの正式原証拠は元 bytes を保持し、根拠ある変換を別の派生物として記録してから扱う。
3. 日付のない OVX は null のまま。snapshot 日付や米国営業日を逆算して指数日付を作らない。
4. reference_date とその証拠、source timezone、利用可能時刻と証拠が不足、暫定値、改定履歴不明は
   `strict_pit_status=EXCLUDED`。必要系列不在は NOT_OBSERVED。データ自体は削除しない。
5. available_at は source_evidence を必要とする。generated_at_only は不適格。
   生成時刻と同じ値でも、独立した公表/取得/計算/依存証拠があれば偶然の同時刻を禁じるものではない。
6. available_at が snapshot 観測/decision より後なら除外。基準日が証拠の利用可能時点より未来なら
   source timezone で除外。銘柄市場日は指定 market timezone で比較し、UTC日付だけで判定しない。
   同じ対象でも market_date と OVX 基準日を等しいと強制しない。
7. calendar の営業日妥当性、米国 release/DST/embargo/依存証拠の実物確認は別審査。
   timezone/calendar 版の宣言だけで正式営業日検証済みとはしない。

## 汚染・欠損・不適格と研究遮断

| 分類 | 主な reason code | 保存／利用 |
|---|---|---|
| 汚染・矛盾 | SOURCE_SYMBOL_MISMATCH、COMPANY_NAME_MISMATCH、COLLISION_WITH_OTHER_OFFICIAL_CODE、OHLCV_IDENTITY_OR_DATE_MISMATCH | raw を保持、研究を遮断 |
| コード破壊の兆候 | ALPHA_CHARACTERS_DROPPED、DIGITS_ONLY_EXTRACTION_POSSIBLE、ALPHA_REPLACED_WITH_ZERO | 原因を断定・修復せず矛盾と共に保持 |
| 欠落・未確認 | TARGET_NOT_OBSERVED、OBSERVATION_INVENTORY_UNVERIFIED、FORMAL_UNIVERSE_OR_SYMBOL_MAPPING_UNVERIFIED、ORIGINAL_OHLCV_UNVERIFIED | 推測補完せず遮断 |
| PIT 不適格 | REFERENCE_DATE_UNVERIFIED、AVAILABILITY_UNVERIFIED、PROVISIONAL_VALUE、REVISION_HISTORY_UNVERIFIED、未来情報 | strict PIT 除外・研究遮断 |
| 探索不明/補完 | EXPLORATION_N_UNVERIFIED、IMPUTED_EXPLORATION_N | 0/1補完せず採用停止 |
| 二重行 | DUPLICATE_OBSERVATION、DUPLICATE_SERIES | 勝手に1件を選ばず範囲全体を遮断 |

`audit_snapshot` は全 row/target/系列の分類と BLOCKED / INDEPENDENT_REVIEW_REQUIRED を返す。
`require_research_preconditions` は入力から再計算して blocker があれば ResearchBlocked。
`audit_received_artifact` は PR #8 store の durable ACK を再検証し、raw hash と事実を結び付け、
receipt/manifest hash と original/derived の親 hash を監査に保存する。
`require_received_research_preconditions` はこの結合と研究遮断を一つの opt-in 呼出しで実施する。
結果 report の flag 書換えで preflight を通す API はない。

全関数が `research_eligible=false` と `facts_authenticity_verified=false` を返す。
矛盾なしでも独立レビューが必要で、利用許可を自動発行しない。
原本 receipt の PROVENANCE_INCOMPLETE / QUARANTINED 状態を変更しない。
raw 破損・隔離・不完全・別原本 receipt は結合前に拒否。派生 report は元 raw と input facts hash に
紐付けた別成果物として append-only に保存し、既存原本や receipt を上書きしない。

この preflight は既存 EML/FROST の全呼出しへ強制接続したものではない。監査を迂回する既存 consumer
全体の実行阻止は保証しない。新受信原本を研究へ渡す前に明示的に実行し、独立承認を待つ設計。
DSR/PBO/N_raw/score/Hard Gate/昇格処理を変更せず、本番・売買にも接続しない。
