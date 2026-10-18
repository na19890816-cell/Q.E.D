# Phase 6 統合回帰の再現手順
[固定HEAD](../reports/phase6_pr_heads_2026-10-18.json)を使用する。
本書は専用ローカル合成環境用。main/実PR/base/Draft/本番に書き込まない。

## DBなし
検証環境はPython3.12.14、pytest9.1.1、numpy2.5.4、pandas3.0.6、psycopg3.3.6、
jsonschema4.25.1。jsonschemaはPR #4のtest-only依存で、新runtime依存はない。
GitHub/proxy/CAの認証設定を維持し、値をログへ出さない。

~~~bash
git fetch origin main \
  +refs/pull/1/head:refs/remotes/phase6/pr1 \
  +refs/pull/2/head:refs/remotes/phase6/pr2 \
  +refs/pull/3/head:refs/remotes/phase6/pr3 \
  +refs/pull/4/head:refs/remotes/phase6/pr4 \
  +refs/pull/5/head:refs/remotes/phase6/pr5 \
  +refs/pull/6/head:refs/remotes/phase6/pr6

git worktree add --detach /path/to/NEW-baseline a3e63907c2f529f289101698ee7d62f8bf72f436
git worktree add -b audit/phase6-NEW-local /path/to/NEW-combined a3e63907c2f529f289101698ee7d62f8bf72f436
~~~

各固定SHAが取得済みであることを確認する。後日PRが変わった場合も固定SHAで再現し、新HEADと混同しない。
baseline treeで以下を実行し、失敗/skipを残す。

~~~bash
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN PYTHONPATH="$PWD" \
  /path/to/venv/bin/python -m pytest -q --junitxml=/path/to/main.xml
~~~

combined treeでは以下の固定commitを順番にlocal mergeする。
~~~bash
git merge --no-ff --no-edit 99604b427716a760a949d85b311f4ecb9728804c
git merge --no-ff --no-edit fbb0ef26a5a5f889dedf9aeed8d858635da68aa0
git merge --no-ff --no-edit 44c974b585f292e91d62de3f7bb8b7b3a3343c11
git merge --no-ff --no-edit c92cef13ed01542145c2e97989efce88f9bd64e5
git merge --no-ff --no-edit 35d670f7181c870cfffd1a5da037e6ad41b66796
~~~

同じDBなしコマンドで#1〜#5結果を保存し、次を追加して再度全体実行する。
~~~bash
git merge --no-ff --no-edit 3bca51dc11779ef51416888e111c620d01cdceb3
~~~

今回のtree hashは#1〜#5 bda32f10ad25cda4b003d1004846c14e9c65e00f、
#1〜#6 5ed8cb89bc0aa2dc93b83597099595d4c75b232a。
local merge commit SHAは日時により変わる。tree/parents/test IDを照合する。
元test ID消失・重複・新失敗を検査する。競合調査はこの専用treeだけで行う。
新しいreplay scriptでも全三段階を実行して件数/ID一致を確認したが、script未コミット版は環境断で回収待ち。

## PostgreSQL
今回はPostgreSQL16.15、image digest
sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4、
Docker network=none、port公開なし、新規専用Unix socket/DBを使った。
local daemonを明示しDocker endpoint/context/TLS selectorを除去、proxy/CAは維持した。

新しい空の別DBをPR #1単独と統合用に用意し、QED_PG_DSNを各専用Unix socketへ設定して実行する。
~~~bash
python -m pytest -q \
  tests/integration/test_adr002_lineage_pg.py \
  tests/integration/test_adr002_s3_manual_pg.py \
  tests/integration/test_adr002_ledger_immutability_pg.py \
  tests/integration/test_trial_snapshot_capture_pg.py
~~~

実ledger機能の順序は084→086→087→088→089。
087は084→087でも成立するが、新S3 clientには088/089が必要。
二回再適用後の全行値/capture replay/同一再送を確認する。
台帳89ケースは外部parent stubなし。late commitでlive as-ofが変化する既存挙動を解消済みとしない。

全体standalone用にはさらに別空DBを作り、QED_MIGRATION_TEST_DSNを指定して
tests/integration/test_migrations_apply_pg.pyを実行する。
その既存fixtureは外部最小stubを使う。親アプリ7件の実schemaやfixtureの代替ではない。

migration後に以下の実repository view DDLも番号順に適用する。
- qedschema/views/060_v_frost_runs.sql
- qedschema/views/061_v_frost_candidate_scores.sql
- qedschema/views/062_v_frost_selection_summary.sql
- qedschema/views/063_v_frost_promotion_status.sql

両DB変数をその同じ専用DBに設定し、以下を実行する。
~~~bash
python -m pytest -q -m 'not parent_app_integration'
~~~

7件はdeselected/未検証と報告する。正式な親migration/dump/fixture版とhash取得後に実結合する。
stub追加やskip化で成功扱いしない。全migration二重適用成功は親互換の証拠ではない。

## SciPy参照
既存2件はSciPyなしでskipする。application venvを変えず別directoryへ
SciPy1.16.3をpip --target/--no-depsで導入し、PYTHONPATHに対象treeとそのdirectoryを指定した。
次のIDはmain/統合双方2成功。さらに同じisolated dependencyを指定した全体PGは2217成功/skip0、7件deselected。
- tests/unit/test_phase4_dsr.py::TestNormalFunctions::test_against_scipy
- tests/unit/test_phase4_dsr.py::TestSampleMoments::test_against_scipy

先行する2215成功/2skipの履歴を書き換えない。baseline再生成/xfail/skip追加はない。

## 追加probeと残る証跡
別の合成probeでC/D相互不変、実writer audit有無、S1/S2/S3合算、copy/restore、
local tail checkpoint、未監査run差分、fsyncを検証した。
当該3script、生XML/ログ、post-restart整合性の確認はCloud復帰後に回収する。
この手順書を未回収scriptのソースそのものと呼ばない。

既存ログ/manifestを保持して作成したcontainer/DB/volumeだけをcleanupする。
今回は環境断後のcleanup完了を確認できていない。
既存branch/main/本番DB/ProStock2原本を削除しない。実データの未送原本は期限だけで削除しない。
