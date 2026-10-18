# Phase 6 統合検証の再現手順

対象は [固定した PR HEAD](../reports/phase6_pr_heads_2026-10-18.json)。
main／既存 PR の ref・base・Draft を変更せず、ローカルに新しい検証 worktree を作る。
以下は正式な production／親 schema への初期化手順ではない。

## DB なしの統合

検証済みは Python 3.12.14、pytest 9.1.1、numpy 2.5.4、pandas 3.0.6、psycopg 3.3.6、
jsonschema 4.25.1。後者は PR #4 の `requirements-contract-test.txt` にある test-only dependency。
新しい runtime dependency を追加しない。プロキシ／CA／GitHub 認証の設定を維持する。

```bash
# repo 内の HEAD ではなく、JSON manifest に固定した各 commit を使用する
git fetch origin main \
  +refs/pull/1/head:refs/remotes/phase6/pr1 \
  +refs/pull/2/head:refs/remotes/phase6/pr2 \
  +refs/pull/3/head:refs/remotes/phase6/pr3 \
  +refs/pull/4/head:refs/remotes/phase6/pr4 \
  +refs/pull/5/head:refs/remotes/phase6/pr5 \
  +refs/pull/6/head:refs/remotes/phase6/pr6

python scripts/audit/run_phase6_regression.py \
  --repo /path/to/Q.E.D --python /path/to/venv/bin/python \
  --manifest docs/reports/phase6_pr_heads_2026-10-18.json \
  --work-dir /path/to/existing-parent/NEW-phase6-replay
```

manifest の commit が local object database にない場合は先に固定 SHA を fetch する。
live PR HEAD が後日変わっても、記録した旧検証と新 HEAD の検証を混同しない。
script は baseline を detached worktree、統合先を `audit/phase6-replay-<uuid>` に作る。
order #2→#1→#3→#4→#5 で全体試験、その後 #6 を追加して再度全体試験を行う。
各 pytest で DB 変数を除去し、PYTHONPATH を対象 worktree のみに固定する。
baseline の既存失敗も残す。統合後失敗・元テスト消失・重複 ID・競合は script の失敗となる。

work-dir に log/XML/result.json と検証 tree を残す。競合したらこの新規 tree で調査し、
既存 PR を retarget/rebase/force-push しない。今回記録した tree hash は次のとおり。

- #1〜#5: `bda32f10ad25cda4b003d1004846c14e9c65e00f`
- #1〜#6: `5ed8cb89bc0aa2dc93b83597099595d4c75b232a`

local merge commit SHA は実行日時により変わり得る。tree hash と pinned parents、テスト ID を照合する。
script の初回検証では local main 未作成の前提を修正し、local main がない checkout では
`refs/remotes/origin/main` を読み取り監視する。修正後の完全 replay は全段階で同一件数・IDを再現した。

## 独立性・ローカル復元・fsync probe

```bash
python scripts/audit/phase6_cross_component_probe.py \
  --tree /path/to/NEW-phase6-replay/combined \
  --work-dir /path/to/existing-parent/NEW-phase6-probe
```

新しい専用 directory に合成原本・C receipt・D event を生成し、既存ファイルを上書きしない。
C/D の相互不変、再送、pretty single JSON の隔離、ローカル backup/restore、tail 削除検出、
未監査 run の registry 差分、4 KiB×64 file の fsync を検証する。
result.json に script 自身の SHA を記録する。実原本、off-host、外部署名、媒体障害は検証しない。

## PostgreSQL の専用試験環境

今回は managed local Docker daemon、`postgres:16-bookworm` の固定 digest
`sha256:0ea6700a3b4f0ae6ce746519073558aed4d88a79d8d07622a9a644946c7319c4`
（PostgreSQL 16.15）を使用した。network は none、外へ公開した port はなく、
新規専用 Unix socket と synthetic DB だけを使った。
Docker の endpoint/context/TLS selector を除去して local daemon を明示し、proxy/CA は維持した。

再現時は専用・未使用の container/volume 名、既存 parent 下の socket directory を用意する。
Docker を使わない場合も、同等の破棄可能な local Unix socket の PostgreSQL を指定する。
control DB `qed_phase6` を新規作成しておく。一般の DSN／本番 DB を指定しない。

```bash
env FROST_LINEAGE_ENABLED=1 python scripts/audit/phase6_migration_probe.py \
  --tree /path/to/NEW-phase6-replay/combined \
  --socket-dir /path/to/owned-test-socket \
  --output /path/to/existing-parent/NEW-migration-result.json
```

probe は新しい `qed_phase6_probe_<uuid>` だけを作成・最後に削除する。
084/086/087/088/089 の prerequisite・再適用・capture と S1/S2/S3 を実 DDL で検証し、
060/061/062/063/082/083 の実 DDL を追加して、FROST audit 有無の実 writer/S2 行・評価・判断も比較する。
pipeline は dry_run=True、昇格実行は0。外部親テーブルの stub は使用しない。
output は既存なら拒否し、別の `.events` directory に合成監査記録を残す。

台帳 89 ケースの比較は PR #1 checkout と統合 checkout のそれぞれに空の別 DB を用意して行う。
`QED_PG_DSN` をその専用 DB にだけ設定し、次を実行する。

```bash
python -m pytest -q \
  tests/integration/test_adr002_lineage_pg.py \
  tests/integration/test_adr002_s3_manual_pg.py \
  tests/integration/test_adr002_ledger_immutability_pg.py \
  tests/integration/test_trial_snapshot_capture_pg.py
```

## 全体 standalone DB 試験と親スキーマ 7 件の境界

別の空 DB で、既存 `test_migrations_apply_pg.py` を `QED_MIGRATION_TEST_DSN` 指定で実行する。
この既存 fixture が使う experiment_runs/audit_events 等の最小 stub は standalone 用であり、
evidence_bundles/factor_candidates の実 schema を再現しない。stub を増やして7件を通さない。

その後、repository の次の実 view DDL を番号順に適用する。

```text
qedschema/views/060_v_frost_runs.sql
qedschema/views/061_v_frost_candidate_scores.sql
qedschema/views/062_v_frost_selection_summary.sql
qedschema/views/063_v_frost_promotion_status.sql
```

migration だけの初期状態では view 未作成による7失敗を実際に再現した。
view DDL 適用後に、両 DB 変数をその同じ専用 DB に設定して実行する。

```bash
python -m pytest -q -m 'not parent_app_integration'
```

親依存7件は deselected と記録し、検証済みにしない。正式な親 migration/dump/fixture の版と
hash を取得してから別の空 DB で実結合する。全migration二重適用の成功は実結合の証拠にならない。

## SciPy 参照2件の補完

既存2件は SciPy がないと importorskip する。primary baseline/replay の環境を変えず、
別 directory へ test-only SciPy 1.16.3 を `pip --target ... --no-deps` で導入し、
PYTHONPATH に対象 checkout とその directory を設定して以下の2 IDを main と統合の両方で実行した。

```text
tests/unit/test_phase4_dsr.py::TestNormalFunctions::test_against_scipy
tests/unit/test_phase4_dsr.py::TestSampleMoments::test_against_scipy
```

双方2成功・失敗0・skip0。primary の2215成功/2skipという履歴は書き換えない。
隔離依存を指定した全体PG実行でも2217成功/失敗0/skip0、親依存7件deselectedを確認した。
既存baselineの再生成、xfail化、skip追加、runtimeへのSciPy導入は行っていない。

## 終了後

log/XML/manifest/result を保存し、作成した container・テスト DB・volume だけを停止／削除する。
worktree と local branch は調査用に残せる。不要になったらこの run が作った path/ref のみを削除する。
ProStock2 の原本、既存 branch/main、実 DB、他のコンテナや volume を cleanup の対象にしない。

Cloud接続断の間はGitHub connectorで監査文書を先に保存した。復帰後にこの手順の3scriptと
生JUnit/logを回収し、SHA付きbundleを追加した。source/backup/restoreのlocal checkpointと
固定capture16件を再取得検証した。fixtureが意図的に保持するzero hashの破損capture1件は
拒否されることを確認し、正常captureへ書き換えない。全体PG再実行も2217成功である。
