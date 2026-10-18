# Phase 7A — 実原本の最小提供仕様と読み取り専用検査

2026-10-18。基準は PR #7 `54061f4`。**実原本 bytes、正式 manifest、データ辞書は未受領**。
以下は正式提供の依頼仕様であり、EC2 への接続・提供承認・形式適合を確認した記録ではない。

## 最小サンプル

提供者は権限・機密性・保持条件・提供範囲を確認し、承認識別子を別の安全な経路で渡す。
認証情報、口座番号、個人情報、取引秘密を GitHub/PR/ログに添付しない。
調査者は承認済みコピーだけを読み取り、元ホストで修正・整形・再符号化しない。

| 報告された所在（存在は利用者報告） | 必要最小限の標本 | 現在の判定 |
|---|---|---|
| `screen/snapshot_*.json` | 連続する市場日2件＋同日別観測版1件（存在する場合）。1文書全体と正式 universe 版 | bytes 未受領。single JSON/JSONL 不明 |
| `screen/candidates_log.jsonl` | 1つの完了 run 全体。棄却・評価失敗・非生存候補を含む。run 境界・全探索性の説明 | bytes 未受領。JSONL としても未検証 |
| `jvram_history.json` | 最小の完結した履歴文書。単位・計算式・市場日・改定の辞書 | bytes 未受領 |
| OVX・レジーム履歴 | 各1完結文書／完了 run。系列名・原公表 timezone・公表/取得/計算の証拠 | 所在の詳細・bytes・辞書未取得 |
| `margin/manifest.jsonl` | 完了 manifest と参照原本1組。対象市場日・銘柄・版・hash の定義 | 参照閉包を含め未検証 |

サイズが大きければ完結した最小原本を選ぶ。切り出し・マスキング・匿名化したものは
**derived sample** と記録し、原本と同一 bytes の実証に使わない。未提供の親原本への hash は
申告参照に留まり、受信済みとは判定しない。匿名化によって値・構造・利用可能時刻の検証が
制限される範囲も記録する。存在しない改定・欠損例を作らせない。

## 提供パッケージ

原本（または承認済み派生標本）の bytes、別ファイルの producer 完了 manifest、
辞書、期待 universe/calendar、時刻根拠、承認・機密区分・保持条件をまとめる。
manifest は原本が閉じられた後に raw SHA-256、byte 長、serialization、安定 artifact/export ID、
source、original/derived、実 export UTC 時刻を固定する。未完了コピーを完了原本と呼ばない。
未知の過去 code/policy/available_at は null と理由で渡し、現在値や mtime で埋めない。

## 読み取り専用チェックリスト

1. 承認済み標本・サイズ上限・保持先・アクセス権を確認。元ファイルは read-only、出力は別領域。
2. 原本を binary で読み byte 長と raw SHA-256 を算出し、producer manifest と一致させる。
   コピー前後の hash が異なれば調査を停止。mtime は履歴時刻の証拠としない。
3. producer の serialization 宣言に従う。single JSON は全体を1文書として、JSONL は物理 LF
   ごとの文書として検証。1行 object は両構文を満たし得るので、拡張子や構文だけで決めない。
4. strict UTF-8、重複 JSON key、NaN/Inf/数値 overflow、末尾の別文書、途中切断、depth/size を検査。
   原本を整形してから検査しない。raw JSON receiver は JSONL を受け付けない。
5. 辞書を受領して初めて論理件数・銘柄・run・観測版・欠損・重複を抽出する。
   物理行数や446銘柄という報告を logical count / universe / N_raw と同一視しない。
6. market_date の市場・意味・calendar、生成/観測/取得/計算時刻と timezone を確認する。
   available_at は原公表＋取得＋計算＋依存原本の根拠を参照し、米国公表時刻の日本市場での
   利用可能性を確認。現在の receiver 時刻を過去 available_at にしない。
7. 独立した期待 universe の対応版・hash と日付×銘柄を照合し、欠落・余剰・不適格を保持する。
   同日改定は別 artifact ID で全版保持し、順序が不明なら不明とする。
8. 当時の code/policy、原 OHLCV、依存証拠の有無を記録。特徴量から原 OHLCV を復元しない。
   regime/candidates は辞書なしに feature v1 へ変換しない。
9. 秘密情報の有無は提供者と限定閲覧者が確認。自動パターン検索を完全な漏洩防止と呼ばない。
   検査結果は安全な code/件数のみ公開し、実 raw・パス・銘柄・hash の公開も別途承認する。
10. manifest/raw/receipt/再送結果を照合し、保管結果と研究適格性を別判定する。

## 契約適合表（bytes 未提供時点）

| 対象 | record v1 | strict JSONL transport v1 | 新 raw JSON artifact v1 | 研究利用 |
|---|---|---|---|---|
| snapshot | 時刻・原市場・code/policy 等未確認 | single document なら不適合。実形式不明 | single document と正式 manifest を確認後のみ審査 | Hold |
| candidates | 専用意味論なし。feature 代入不可 | legacy archive としても実 bytes 検査待ち | JSONL 原本は対象外 | Hold |
| JVRAM / OVX | 系列・単位・availability 辞書待ち | 実形式不明 | single document の場合のみ審査 | Hold |
| regime | カテゴリ・遷移・選抜の意味論なし | 実形式不明 | 保管契約のみ。意味論検証ではない | Hold |
| margin manifest | manifest と原本参照の専用意味論待ち | JSONL 検査・参照閉包とも未確認 | JSONL 原本は対象外 | Hold |

実適合 **0件判定**。新契約の実装・合成試験が成功しても、この表を適合済みに変更しない。
