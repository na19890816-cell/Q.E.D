# Phase 3 — NOTE-008 実データ所在監査（2026-10-17）

**Hold: 実データ・全探索N・OOS returns未取得。採用評価を停止する。**
main `a3e6390` 起点の独立ブランチで、PR #1/Phase 2の変更を含めない。
本Phaseは所在監査と検証契約の記録のみ。実データgolden固定、仮説A/B/C実測、統計検定は未実施。
実データ不足を合成結果で埋めず、現行方式を維持する。

## 実施した調査

| 対象 | 確認結果 |
|---|---|
| NOTE-008既存数値 | frost_meta_validation_synthetic_2026-10-03.mdが合成120候補、候補seed7と明示。実運用の証拠ではない |
| 実候補・評価・選抜履歴 | 可視workspaceに承認済みexportを発見できず。ローカルDBは機能テストfixtureなので使用しない |
| golden dataset/baseline | git追跡済みのdataset CSV/baseline.jsonなし。既存値を新規生成・再生成していない |
| Makefile.golden | Event Studyのevent_study_summaries CSV抽出用。P8 FrostCandidate JSONとは対象・形式が異なる |
| golden_check | *_id / *_at等を除外。時刻・run・candidate provenanceの原本を代用しない |
| P8 JSON loader | created_at/updated_atを除外し、FrostCandidate外の情報を落とす。監査原本をそのまま渡す経路ではない |
| P8 dataset_hash | 評価入力10項目が対象。run/family/policy/観測時刻/探索N全体を結び付けない |
| FROST schema | run・候補・評価・決定・config・policy情報を持つがUPSERTあり。created_at/updated_atだけで過去可視性を保証しない |
| 全探索N/非生存候補 | 未取得/網羅性未確認。n_candidatesやn_runsをN_rawへ代入しない |
| returns/CPCV/確認期間 | 未取得。既存fold Sharpe近似を完全CPCVと報告しない |

GitHub上で参照先親repoは現資格で404、認証付き/userとQ.E.Dは成功。
接続障害という断定ではなく、正確な取得先・固定版・アクセスが不足している。
実履歴の所在はユーザーにも照会したが、ここまでに取得できた実データはない。

## 仮説と統計検証の状態

実データでregime_stabilityの定数化、top_k非拘束率、±20%摂動の順位/昇格境界を測定していない。
実SELECTED・昇格候補・人間承認済み昇格集合も未取得。
コード上の `promo_jaccard` はGate通過スコア上位集合の一致であり、実昇格そのものの一致ではない。
Gate通過数≤top_kでもfamily上限・duplicate抑制・最終ポリシーは別に作用するため、NOTE-008の断定を条件付きに訂正した。
regime軸の定数性も全レジーム観測済み・現閾値という条件へ限定した。
欠落時には比率1/2やNoneも取り得て、Noneは当該gateで拒否されずscore0.5になる既存経路がある。
全通過集合での定数化を確認したとは扱わず、欠落率・欠落群の通過/選抜も実データで測る必要がある。
この既存gateの欠落許容は変更していない。

候補モデル数・予定重み設定総数・予定閾値設定総数・歴史的N_rawは不明/未固定。
今回の新しい研究実行は0回。A0〜A3は比較案であり、実行済み4モデルとは数えない。
全試行数と確認期間を固定できないため、完全な事前登録が済んだとも扱わない。

Kendall τ-b分布、Jaccard分布、p05/p50/p95、ポートフォリオ相関、コスト後OOS SR、DSR/PBO、
regime性能、DD/turnover、昇格候補への影響、置換p値、効果量、CIは全て **未算出**。
有意差なしを確認したわけではなく、未検証のため現行方式を維持する。
必要な式・対照・リーク防止・CPCV/block bootstrap設計の前提を[検証契約](../runbooks/frost_meta_real_data.md)へ記録した。

## 検証・変更範囲

実行したのはgit/ファイルの所在確認、対象コードとschemaの読取り、既存合成報告の出典確認、JSON/内部リンクの静的検証。
実データ統計実験0、pytest新規実行0（文書・状態JSONのみの変更のため）。
既存Phase 2のmain比較結果を本ブランチで再実行した成功件数として流用しない。
JSONのN/測定結果はnull、実行数は0、adoption_evaluation_stopped=trueとし、不明を0試行や成功へ変換していない。
FROSTコード、policy、DSR/PBO、goldenモジュール、既存baseline、DBは変更していない。

変更ファイル:

- `docs/notes/NOTE-008-p8-meta-validation-structural-findings.md`
- `docs/runbooks/frost_meta_real_data.md`
- 本報告と同名の状態JSON

## 再開条件・次の優先順位

1. 全run・非生存候補を含む実履歴の正式exportとschema/config/code版、観測可能時刻の根拠を取得する。
2. 原探索ログとADR-002により全探索Nを照合し、研究設定数・全実行数・確認用期間を固定する。
3. immutable原本のhash付きmanifestをレビューし、p95等の観測器不足を別途実装・回帰検証してから観測実験へ進む。
4. 実データでPhase 3を評価した後、NOTE-004/005を独立Phase 4でGate-0検証する。

**Go**: 取得契約のレビュー。**Hold**: 実データgolden固定、A/B/C実験、採用評価、Phase 4 Gate-0。
Phase 4ではシグナル数の意味を先に定義し、63/126/252を事前比較、train/validation SRの期間/頻度/年率化を揃え、
PBOへの追加説明力を検証する必要がある。現時点では未着手・未検証で、新Hard Gateを追加していない。
Gate-0で増分効果が消える案はRejectとし、有意な改善がない場合は現行方式を維持する。
