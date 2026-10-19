# Phase 8C — 実原本の正式提供後の受入手順

2026-10-19。実 bytes、Producer 完了 manifest、正式 universe、取得 symbol の原証拠は未受領。
以下は検証手順で、実受信・実銘柄同一性・実 PIT の成功記録ではない。実相互試験は Hold。
PR #8 の最小サンプル仕様に加え、以下を取得する。本番/EC2/DB/cron/売買へ接続しない。

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

## 実提供後の順序

1. 読み取り専用コピーと完了 manifest の raw SHA/byte 長/serialization を照合し、機密性を確認。
2. single JSON は raw 全体として strict UTF-8/JSON を検証。非ASCII、改行、空白を改変しない。
   複数文書 JSONL を single JSON と誤読しない。
3. 専用ローカル root の PR #8 RawJsonArtifactStore で受信。durable receipt と manifest hash、
   原本 hash/size、取得時刻を照合する。原本保管と研究利用の判定を分ける。
4. 同一 manifest の再送、1byte 改変の隔離、同一市場日別revisionの別ID保存、original/derivedの
   親参照を相互確認する。Producer が receiver ACK を検証する実相互試験は標本提供後に実施。
5. 正式辞書で定義した抽出仕様を固定してから監査事実を作る。未知 JSON key の推定や数字抽出で
   code を修復しない。完全な抽出一覧がない場合、対象不在は NOT_OBSERVED とせず範囲未確認。
6. `audit_received_artifact` で原本 receipt に結び付け、会社名・code・実取得 symbol と正式対応を
   照合。別銘柄の価格は IDENTITY_MISMATCH。保存されていない対象は範囲確認後 NOT_OBSERVED。
7. market_date / OVX reference_date / snapshot observed_at を独立に照合。日付なし OVX、暫定、
   改定履歴不明、availability 証拠なしを strict PIT から除外する。timestamp を推測生成しない。
8. `require_received_research_preconditions` を実行し blocker を研究前に拒否。矛盾なしでも
   research_eligible は false。正式原証拠・権限・完全性・統計条件の独立承認を別途必要とする。
9. report は原本 hash/receipt hash/input facts hash と共に別の派生監査記録へ保存。raw/receiptを
   上書きしない。実原本や秘密をログ/PRへ出さず、必要な code/件数の公開範囲を承認する。

## 今回の実証範囲

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
