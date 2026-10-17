# FROSTメタ検証: 実データ取得と再開条件

2026-10-17時点は **Hold**。この文書は必要条件であり、実データ検証の実施記録ではない。
合成fixture・ローカル統合テストDBを実候補履歴へ代用しない。現行ポリシー・DSR・PBO・Hard Gateは維持する。

## 必要な原本

固定する単位はrun全体。SELECTEDや生存候補だけを抽出せず、HOLD/REJECTED/REVIEW_REQUIRED、
未評価・欠落の理由、探索段階で未保存の試行も原探索ログと照合する。

| 情報 | 原本・確認事項 |
|---|---|
| candidate_id / run_id | frost_runs / frost_fitness_candidates / frost_evaluations / frost_selection_decisionsをrunとcandidate両方で対応付ける |
| family_key | 元family_spec・元探索run・ADR-002 batch/edgeとの対応。source_candidate_idとfamily_keyを同義扱いしない |
| policy_hash / config | run当時の完全config・コードcommit・policy本文とhash。現環境のデフォルトで置き換えない |
| 10軸・全ゲート | 生指標・軸スコア・各gate pass/fail・欠落理由・v1/v2の区別 |
| DSR / PBO / OOS SR | 元returns・頻度・年率化・N・帰無モデル・計算版と照合。欠落値を0で埋めない |
| regime | regime_pass_ratio、各regimeの期間・標本数・Sharpe、分類モデル/データ版 |
| 決定・順位 | 全4決定区分、rank、top_k、promotion_top_k、family/duplicate制約、最終eligibilityと人間の承認履歴 |
| 時刻 | 観測可能時刻・評価/決定時刻・データ締切・提出/保存時刻を区別。created_at/updated_atだけで当時の可視性を証明しない |
| 全探索N | 未保存試行を含む原ログ、モデル/重み/閾値探索数、ADR-002 N_rawとbatch ID全件。候補数や摂動回数で代替しない |
| 経済指標 | コスト後の日次OOS returns、ポートフォリオ構成、DD、turnover、価格/コスト/取引カレンダー版 |
| 分割 | train/validation/独立確認期間、CPCV purge/embargo、重複保有・系列相関を考慮するblock定義 |

実データの正式な読み取り接続または承認済みexportが必要。秘密値をPR/ログへ書かない。
接続が得られたらREPEATABLE READ / READ ONLYの一貫したsnapshotで全対象runを抽出し、
取得時刻・選択SQL・除外条件・件数・欠落・source schema版を記録する。
UPSERTで上書きされた過去値は最新行から推測せず、immutable export・イベントログ等の原本がなければ再現不能と明示する。

各原ファイルにSHA-256と件数を付け、manifestに入力・policy・コード・Nの根拠を結び付ける。
既存 `dataset_hash` だけではrun/family/policy/時刻/探索Nのprovenance全体を結び付けない。
golden_checkが除外するID/時刻も研究用原本では保持する。元のgolden baselineを再生成しない。
初回データ固定はレビュー対象にし、既存baselineの更新や本番DB保存は別途承認を得る。

## 実験前の登録

データ取得後、結果を見る前に候補モデル数・重み設定数・閾値設定数・原探索N・今回の全研究試行数を固定する。
現時点では歴史的Nも設定総数も未取得/未固定なので、完全な事前登録が済んだとは扱わない。
既存の120候補、50摂動、今回の実行0回は原探索Nではない。
確認用期間を別に保管し、同じOOSで反復最適化して都合のよい設定だけを残さない。

観測専用の比較案:

- A0 現行重みを対照とする。A1 定数軸除去、A2 連続regime指標、A3 gate閾値変更を比較候補とする。
  A2の変換式・尺度、A3の閾値grid、重み再配分、欠落処理を事前固定し、試した全設定をNへ記録する。
  regime全観測/一部欠落/全欠落を分け、ratio=Noneの既存gate/score fallbackを含めて定数化を検証する。
- runごとにGate通過数 `g_r` とtop_k `k_r` を記録し、件数上限のbinding率 `mean(1[g_r > k_r])` を出す。
  family/duplicate制約と最終ポリシーも別途記録し、この率だけで重みが無関係とは判定しない。
- SELECTED集合、単純スコア上位集合、最終昇格候補、人間承認済み実昇格を分けて照合する。
  Jaccardは `|A∩B|/|A∪B|`、両集合空は既存定義1とするが、空集合率も併記する。
- ±20%重み摂動を中心にseedを事前固定し、Kendall τ-b、promotion Jaccard、境界スコア差のp05/p50/p95をrun別・seed別に報告する。
  既存summarize_distributionにはp95がないため、実測前に観測器の拡張・回帰検証が必要。
- 同一評価条件の対照で、順位、ポートフォリオ相関、コスト後OOS SR、DSR/PBO、regime性能、DD/turnover、昇格候補への影響を測る。

CPCVには候補×時系列returnsと明示的なpurge/embargo分割が必要。
既存fold Sharpe近似を完全なCPCV実施と報告しない。
候補/run間の依存を考慮した時系列block bootstrapと対応のある置換検定を設計し、
block長・反復数・seed・多重比較補正・最小経済効果量を実験前に固定する。
主評価指標、帰無仮説/検定統計量、片側/両側、CI水準、比較単位（run/期間等）も固定する。
p値、対応差の効果量、信頼区間、サンプル数/有効block数、検出力の限界を報告する。
データ/標本不足ならHold、有意な改善が確認できなければ現行方式を維持する。
観測結果だけで自動昇格・gate変更・新baseline採用はしない。

## 停止と次のフェーズ

全探索N不明、時刻/元returns不明、生存者だけの抽出、確認用期間の混用、schema/provenance不明のいずれかがあれば採用評価を停止する。
現在は実データ・N・returns未取得に該当し、A/B/Cの数値、p値、効果量、CIは未算出。
Phase 4のNOTE-004/005 Gate-0も実施しない。実結果を得るまで新Hard Gateを追加しない。
