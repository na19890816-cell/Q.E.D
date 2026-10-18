# Phase 5R Task D — 新規探索の全試行保全

2026-10-17。比較基準は main `a3e63907c2f529f289101698ee7d62f8bf72f436`。
Task C と独立した `feat/phase5r-exploration-audit` branch。PR #1〜#4 の差分を含めない。
本記録は実装・監査可能性の評価であり、NOTE-008 の採用評価ではない。

## 監査結果と対応

| 対象 | main の保存漏れ / 制約 | 本変更 |
|---|---|---|
| EML top_k 前 | eml_search の全木・fitness はメモリだけ。SearchStats は終了後のみ | 生成観測・評価前 start・全結果・top_k disposition を永続化 |
| EML 勾配 | finite difference の出力・失敗は消える | probe の全 tree/weight 証拠と評価結果を参考 event に保存。独立 N にしない |
| EML 安全式 / 重複 / 昇格閾値 | master が戻さない原候補がある | 各除外・残存・判断を記録し、参照評価も別保存 |
| EML 例外・NaN/Inf | -999/集計へ変換され原状態を失う | FAILED/NONFINITE/INVALID を区別し、未知・非有限を 0 に補完しない |
| EML 実効環境重み | config 外の EML_PROMO_W_* が未固定 | 実効値を開始時に保存。参照評価前の変更で停止 |
| FROST 棄却候補 | 正常時は既存 DB 保存対象だが UPSERT で履歴上書き | 全投入候補・評価・棄却を別の immutable event で保存 |
| FROST 途中失敗 | batch list が完成前に例外となると先行成功も output に載らない | 全入力を最初の評価前に登録。先行成功・失敗・未訪問を区別 |
| FROST family | 未指定時は既存 S2 の環境既定へ帰属 | 同じ実効既定と default 補充の有無を記録。上流根拠解決とは扱わない |
| 重み / 閾値研究 | 重み設定・個別順位は分布に集約され消える | baseline・全設定を計算前に固定し、各 snapshot/metrics を保存 |
| 中断 / crash | 保存前候補・終了 N が不明 | fsync 済み開始と未完了 ID を再起動後に inspect。N を推測しない |

追加レビューで発見した family 環境既定の不一致、EML 参照評価の -999/NaN を SUCCEEDED と
していた監査分類、実効環境重みの不足を修正し、それぞれ回帰 fixture を追加した。
既存の数値計算・DSR・PBO・Hard Gate・昇格規則は変更していない。
audit=None の callback 例外は従来の fail-sentinel を維持することも確認する。

## 仕様・変更ファイル

永続化・識別・N_raw・障害範囲は
[探索イベント監査仕様](../architecture/exploration_event_audit.md) に記載した。

- `analytics/python/audit/__init__.py`
- `analytics/python/audit/exploration_events.py` — local fsync / no-overwrite event writer・検証・inspect
- `analytics/python/alpha/eml/eml_search.py` — optional 全生成/評価/probe/disposition
- `analytics/python/alpha/eml/eml_master_formula.py` — optional 有効設定・重複/安全式/参照評価/判断
- `analytics/python/frost/frost_selector.py` — optional 全入力・逐次結果・失敗/未訪問
- `analytics/python/frost/frost_runner.py` — optional run policy 照合・最終判断・監査障害の停止
- `analytics/python/frost/frost_meta_sensitivity.py` — optional baseline・全研究設定・個別結果
- `scripts/audit/inspect_exploration.py` — DB なしの整合性/未完了 CLI
- `tests/unit/test_exploration_events.py`
- `tests/unit/test_exploration_meta_audit.py`
- `tests/integration/test_exploration_audit_local.py`
- `docs/architecture/exploration_event_audit.md`
- 本記録

## 回帰テスト

最終結果は同一 Cloud 環境の XML と failure ID 比較から記載する。
新規テストと既存 S1/S2/P8 を実行し、全体テストは QED_PG_DSN と
QED_MIGRATION_TEST_DSN を unset して baseline と変更後を比較する。
DB なし試験の既存 skip は成功扱いしない。新規 skip/xfail は追加しない。

| 検証 | 成功 | 失敗 | skip | exit |
|---|---:|---:|---:|---:|
| 新規 local audit 単体・合成統合 | 83 | 0 | 0 | 0 |
| 新規 + 既存 S1 / S2 / P8 対象 | 197 | 0 | 0 | 0 |
| main baseline 全体（DB なし） | 1606 | 3 | 70 | 1 |
| 変更後 全体（DB なし） | 1689 | 3 | 70 | 1 |

失敗 ID は前後で完全一致、新規失敗 0。既存 3 件は全て
`tests/unit/test_phase6_base_writer.py::TestFrostWriterImport` の
`test_module_imports_safe_float`、`test_module_imports_safe_float_opt`、
`test_module_imports_safe_json` で、固定 `/home/user/prostock` のファイル不在。
修正は独立 PR #2 にあり、この main 起点 branch へ混入させない。
既存 70 skip は DB 接続未設定・scipy 未導入などで、実 DB 結合成功を意味しない。
合成 fixture の定数系列での NumPy warning と既存 pytest fixture warning は残る。

再現コマンド（リポジトリ root。baseline も同じ Python 環境、PYTHONPATH だけ checkout を変更）:

```bash
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN PYTHONPATH="$PWD" \
  /workspace/Q.E.D/.venv/bin/python -m pytest tests -q -o addopts= --tb=short -rs
PYTHONPATH="$PWD" /workspace/Q.E.D/.venv/bin/python -m pytest \
  tests/unit/test_exploration_events.py \
  tests/integration/test_exploration_audit_local.py \
  tests/unit/test_exploration_meta_audit.py \
  tests/unit/test_adr002_s1_search_stats.py \
  tests/unit/test_adr002_s2_frost_ledger.py \
  tests/unit/test_p8_meta_sensitivity.py -q -o addopts= --tb=short
```

Python 3.12.14 / pytest 9.1.1。各実行は JUnit XML を出力し、testcase の
failure/error ID 集合を比較した。XML/生ログは今回の workspace 中間記録、
GitHub 保存用の件数と ID は
[比較記録 JSON](phase5r_task_d_test_comparison_2026-10-17.json) に記載する。
`git diff --check` も成功。

## N_raw と既存系譜の保証範囲

S1 は探索 stage/run ごとの異なる最終式、stage 横断・独立 run 横断は別試行。
optimizer probe と full evaluation は追加 N にしない。
S2 は既存どおり EML を除外し、非 EML の family/run ごとの異なる hash を計上する。
監査側は expected owner を記録するだけで、`ledger_write_verified=false` とする。
S3 との再投入・二重計上を避けるため、audit から台帳への自動 export は実装しない。
重み/閾値/family 研究は全設定を保全し、独立試行としての N への帰属判断を自動化しない。
過去 N、未完了探索の最終 N、S1/S2/S3 間の未取得証拠は null/未確認のまま。
同一 audit root では run_id を排他的に予約し、別 search での同 run 再評価を拒否する。
同じ audit object の stage 間伝播は可能。別 root・既存非監査 run との衝突、および
既存 S1/S2 の同 run 異証拠への DO NOTHING 制約と跨る完全照合は未解決。

## 永続保存と未対応障害

対応する opt-in 入口から原入力 hash → 開始設定 → trial start → 評価/失敗 → 終了 count
まで保存できる。評価 callback の前に start の fsync が終わる。
実プロセス `os._exit` による中断後も開始済み ID を再読できることを合成 fixture で検証する。
同 attempt の再作成は拒否し、保存済み原本を再取得する。元ファイルを上書きしない。
事前に永続 provision された root を使用し、祖先を再帰作成しない。
read/inspect でも全 event と directory を再 fsync し、ACK 喪失後の可視ファイルを
耐久性未確認のまま成功扱いしない。再 fsync の失敗も拒否する。
最大の保証は local POSIX の fsync/link/flock が正常に実装された単一 producer でのもの。

未対応は計算の途中再開、N の自動補修、DB との分散 transaction、WORM/外部署名、
NFS/object storage、全末尾削除・所有者による完全 chain 再生成、fsync を偽る媒体、
raw market data/returns の別保管、audit 未指定の経路や callback 内部の独自研究である。
追記中の過去ファイルの bit rot は最終/独立 inspect まで検出しない。
保存容量・inode・IOPS の上限監視と off-host backup は rollout 前の要件。
エラーメッセージや DSN を event/CLI に記録せず、credential-like な設定 key を拒否する。
全任意文字列の秘密自動判別を保証する機能ではない。

## 依存・データ要件と Go / Hold

Go: 合成データによる opt-in ローカル監査と、新規研究をこの入口で行うためのソフトウェア基盤。
Hold: 本番 rollout・実 FROST/EML 履歴の完全性・NOTE-008 の統計的採用。
実データ、全探索 N、policy/code 履歴、available_at、コスト後 OOS returns は未取得で、
p 値・効果量・CI・CPCV・bootstrap による alpha 検証は実施していない。
Task C / ProStock2 Exporter と将来連携する際は、入力 evidence hash の原本所在と
契約版、開始設定の policy/family、欠落候補、時点整合性の照合が必要。
既存 EC2・cron・本番 DB・取引システムへの接続は行わない。
親スキーマ未取得の外部依存 7 件は未検証 Hold のまま。

次の優先順位は、正式な入力/全探索履歴の受領、run ID 再利用禁止と ancestry 帰属の確認、
永続ディスク/バックアップの計測、独立した監査 enabled 運用の承認である。
main マージ、Draft 解除、実売買への接続は行わない。
