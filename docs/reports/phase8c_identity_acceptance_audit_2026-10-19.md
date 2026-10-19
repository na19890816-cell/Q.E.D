# Phase 8C — 実原本受信・銘柄同一性監査

2026-10-19。**Go: 独立した監査・研究前preflightの合成ソフトウェア検証。Hold: 実原本相互試験・実研究・本番接続。**
実 raw bytes / Producer 完了 manifest / 正式 universe / 取得 symbol 証拠 / 原 OHLCV / 時刻根拠は未受領。
証券コード汚染は利用者報告の問題を合成反例で再現したもので、実履歴の汚染件数は確認していない。
実 EC2・本番 DB・cron・SSH 鍵・売買・crisisEngine への接続・変更は行っていない。

## 基準・依存関係

main: `a3e63907c2f529f289101698ee7d62f8bf72f436`、変更なし。
本 branch は `feat/phase8c-identity-pit-audit`、新 Draft PR の base は PR #8 の
`feat/phase7-raw-json-artifact`、固定 HEAD `2d2d5dc1b7c4cb199a7cf63157641c0cc4ee498a`。
直接依存 #8 → #5 → #4。既存 PR #1〜#8 の Draft/base/HEAD は変更しない。
試験対象実装 commit: `b3f062608ac283fc3abf734e9296184961ec9edc`。
後続 commit は受入手順・監査報告・生証跡の追加で、実装・試験・既存 baseline を変更しない。

使い捨て worktree では従来の #2→#1→#3→#4→#5→#6→#7→#8 に本実装をローカル統合し、競合なし。
統合 branch を push していない。新 PR の推奨位置は #8 の後。将来の親 PR merge に伴う retarget と
差分再確認は人間の承認後に別途行う。main merge・既存 PR の履歴書換えは実施しない。

## 実原本・対象識別子の判定

| 対象 | 実原本 bytes / 正式対応 | 実対象の監査 status |
|---|---|---|
| 285A / 2850 | 未提供 | NOT_ASSESSED_NO_ARTIFACT |
| 417A / 4170 | 未提供 | NOT_ASSESSED_NO_ARTIFACT |
| snapshot / OVX 履歴 | 時刻・基準日・改定の原証拠未提供 | 実 PIT 判定 Hold |
| 原 OHLCV / 全探索 N | 取得・照合先未確定 | 原本照合・採用判定 Hold |

実原本がないことを「対象が保存されていない」とは扱わない。NOT_OBSERVED は、受領した特定原本の
完全一覧と抽出仕様を確認してから、その範囲で完全一致 code がない場合のみ使用する。
一部標本や辞書不明は OBSERVATION_COVERAGE_UNVERIFIED、正式 universe / 取得 symbol 未確認は
IDENTITY_UNVERIFIED。実相互試験の実施件数は **0**。

## 実装と分類

`identity_pit_audit.py` に既存 wire 契約と独立した内部監査モデル/純関数/preflight を追加。
実 snapshot の field 構造は推定せず、正式辞書で抽出・レビューした明示事実を入力とする。
`285A != 2850`、`417A != 4170`。英字脱落、0置換、数字抽出の兆候を検出し、自動修復しない。
正式時点別の会社名/provider/symbol と矛盾する保存行は IDENTITY_MISMATCH。
正式 code への衝突、Unicode類似表記、case、空白、整数化も同一視しない。
対象 code が無ければ、別 code の会社名を使って対象が観測されたと数えない。

snapshot observed_at、銘柄 market_date、OVX reference_date と source timezone を分離。
generated_at / filename / mtime / receipt 時刻から available_at や指数日付を作らない。
基準日・利用可能性証拠・timezone の不足、暫定値、改定履歴不明は strict PIT から EXCLUDED。
未来の市場日/利用可能時刻、原 OHLCV の code/provider/symbol/date 不一致、未照合 OHLCV、
未知/推測補完 N は研究入力を遮断する。N_raw の数え方自体は変更・再計算しない。

原本保存は PR #8 のまま。新 `audit_received_artifact` は durable ACK の raw/receipt/manifest hash を
再検証し、監査事実の raw hash と original/derived・親 hash を結び付ける。
隔離・破損・不完全・別原本 receipt を拒否し、既存 receipt を昇格・上書きしない。
`require_received_research_preconditions` は receipt 結合と blocker による ResearchBlocked を実行する。
入力から再計算するため、report の flag を書換えて通す API はない。

全ケースで research_eligible=false / facts_authenticity_verified=false。
矛盾なしでも INDEPENDENT_REVIEW_REQUIRED。raw SHA や caller の正式確認 flag、照合 hash だけでは
原証拠の真正性・完全性・利用可能時刻を証明しない。正式提供/原 OHLCV 比較/履歴照合の実証は未実施。
この opt-in preflight は既存全 consumer への強制接続ではなく、監査迂回の全体阻止保証はない。
新受信データを既存 FROST・売買へ接続せず、現データの実研究利用は Hold を維持する。

## テストと失敗 ID 比較

同一 Cloud 環境、Python 3.12.14 / pytest 9.1.1 / IANA system tzdata 2026b。DB 環境変数を除去した
no-DB 実行。新規80件（単体71、ローカルfilesystem統合9）は成功、失敗/skip/xfailなし。

| 比較対象 | 成功 | 失敗 | skip | 実 exit code |
|---|---:|---:|---:|---:|
| PR #8 HEAD、変更前 | 1957 | 3 | 70 | 1 |
| PR #8＋本実装、変更後 | 2037 | 3 | 70 | 1 |
| 既存契約・受入＋新規対象 | 431 | 0 | 0 | 0 |
| PR #1〜#8統合、追加前 | 2149 | 0 | 156 | 0 |
| PR #1〜#8＋本実装、追加後 | 2229 | 0 | 156 | 0 |

前後とも新規失敗 ID / 追加skip ID / 削除test ID / 重複test ID は0。skip は成功に加算しない。
PR #8 単独基準の既存3失敗は同じ固定パス依存で、#2 を含む統合では成功する。

```
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_float_opt
tests.unit.test_phase6_base_writer.TestFrostWriterImport::test_module_imports_safe_json
```

PG 試験は本 Phase で再実行していない。正式親スキーマ実結合7件は引き続き Hold。
スタブによる実結合成功を主張しない。DSR/PBO/score/Gate/promotion/N_raw の保護対象10 source、
既存 record/JSONL/raw artifact の validator/schema 5 file の hash 不変を確認した。
既存 file の変更、新依存ファイル、migration、baseline 再生成はない。

受入回帰には raw SHA/size/single JSON、実 UTF-8非ASCII保持、原子的保存/fsync/receipt、再送、
1byte改変、同一市場日別revision、original/derived、SIGKILL/再起動/容量不足等の既存障害試験を含む。
新規試験は保存成功でも汚染・欠損・PIT不適格・未知Nを研究前に拒否し、元 bytes/receipt を保つことを確認。

## 合成の再現可能な証跡

`phase8c_identity_pit_probe.py` を実行し、10シナリオを保存した。
入力間整合の対照1件、285A/417Aの別symbol、0置換衝突、不在、正式対応未確認、日付なしOVX、
未知N、補完N、原OHLCV未照合。対照も研究許可を付与せず、残り9件は preflight で遮断した。
原本 bytes・完了 manifest・明示監査事実・immutable receipt・派生reportを別保存し hash を対応づけた。
fixture の企業名、provider、hash、N、比較確認 flag は全て合成。実企業・実指標の確認ではない。

- 生証跡: `evidence/phase8c_2026-10-19.tar.gz`、91 files、157618 bytes。
- archive SHA-256: `de48a6a36052742a0f31edef5cb65bd766614d514e057bf637c580e838cf962a`。
- `phase8c_evidence_manifest_2026-10-19.json` に全 file SHA/size。
- `phase8c_validation_2026-10-19.json` に全 failure/skip ID、版、保護 hash、PR HEAD、合成分類結果。
- credential pattern scan は該当なし。完全な機密情報検出器・実 raw 公開の承認ではない。

## 変更ファイル

- `analytics/python/data_contracts/identity_pit_audit.py`
- `tests/fixtures/identity_pit_audit.py`
- `tests/unit/test_identity_pit_audit.py`
- `tests/integration/test_identity_artifact_audit.py`
- `scripts/audit/phase8c_identity_pit_probe.py`
- `docs/contracts/identity_pit_classification_v1.md`
- `docs/runbooks/phase8c_real_sample_acceptance.md`
- `docs/runbooks/phase8c_validation_replay.md`
- 本 report、validation JSON、evidence manifest、evidence archive

## 残る障害・権限・取得問題と次の判定

最優先は正式提供者の承認を伴う最小原本/完了manifest/時点別universe/実取得symbol/辞書の取得。
その後に銘柄別原OHLCV照合、OVX基準日・利用可能性・改定、全探索Nの根拠を確認する。
過去不明値や未保存 OHLCV を推測で埋めず、不足が続けば研究は Hold。
親原本未提供の匿名化標本は derived の参照閉包を満たさず、original 偽称で受理しない。

formal flag・OHLCV比較・ledger_verified の真実性、抽出仕様の正しさ、正式営業日検証、監査を迂回する
既存 consumer の全体検出・認証・独立承認は未保証。受信の local POSIX 保証を越える媒体故障・
電源断・off-host backup・外部署名/WORMも今回未検証。実資源測定・本番導入を行わない。
**実データ相互試験・NOTE-008採用・新Hard Gate・本番接続は Hold。**
既存 DSR/PBO/FROST/Hard Gate/昇格を変更せず、既存 PR の Draft 解除・main merge は行わない。
