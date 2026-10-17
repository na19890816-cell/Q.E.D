# Q.E.D テストDB: standalone と親アプリ実結合

本番DBを指定しない。接続文字列は安全な環境設定で渡し、ログ・コマンド引数・PRへ認証値を書かない。
main起点のPhase 2で整備する手順で、PR #1の機能migrationは含めない。

## 二つの検証範囲

`test_migrations_apply_pg.py` は外部の `experiment_runs` / `audit_events` 等の最小stubを明示的に使う。
これはQ.E.D単独のmigration構文・再適用検査であり、親アプリとのschema互換・実結合の成功ではない。
stubを拡張して実結合7件を成功扱いしてはならない。

`parent_app_integration` はEvent StudyのPhase B/C/Dと全pipeline再実行の7件。
正式な親schemaとfixtureが必要で、デフォルト実行から除外していない。
有効なfactor_candidateがない場合も失敗とし、fixture不足をskipへ変換しない。

```bash
# DBなし: 実DBテストのskipは成功件数と分けて報告する
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN python -m pytest tests/ -q

# Q.E.D単独の範囲。除外7件はdeselectedであり、検証済みではない
python -m pytest tests/integration/ -m 'not parent_app_integration' -q

# 実結合7件。依存不足時の失敗を残す
python -m pytest tests/integration/ -m parent_app_integration -q
```

## 実結合の初期化順序

1. 親アプリの正確なrepository URL・commitを固定し、元のmigration/公式schema-only dumpとchecksumを保存する。
2. 破棄可能な専用PostgreSQL DBを作り、その親の公式初期化手順を実行する。FK参照先、CHECK、INDEX、DEFAULT、trigger関数も含める。
3. そのDBへQ.E.Dのmigrationを番号順に適用する。最小stubの既存テストを実schemaへの初期化の代わりに実行しない。
4. 親アプリの正式fixtureをロードする。少なくともactiveなfactor_candidateと必要な参照先を含み、fixture版・checksumを固定する。
5. `QED_PG_DSN` をその専用DBに設定し、次の読取専用観測を保存して固定DDLと比較する。

```bash
python scripts/postgres/inspect_parent_schema.py > parent-schema-observation.json
python -m pytest tests/integration/ -m parent_app_integration -q
```

観測CLIは接続文字コードUTF8、REPEATABLE READ / READ ONLYでcatalogの列型・NULL・DEFAULT・制約・index・trigger定義を取得する。
テーブル欠落は終了コード2、接続失敗は1。0は観測完了だけを意味し、常に `integration_verified=false` を出力する。
存在する列を見ただけでは正式schemaと一致したと判定しない。親DDL版との照合と実結合テストが別途必要。
triggerが参照する関数本体・推移的依存先は、この観測だけでは網羅しない。正式dumpに含める。

## 現在の障害（2026-10-17）

Q.E.Dには `evidence_bundles` / `factor_candidates` の実DDLがない。
docsで参照された `na19890816-cell/prostock-bff-v2` と旧作業名由来 `na19890816-cell/prostock` は現資格でHTTP404。
GitHub /user とQ.E.D取得は成功したため全体認証の失敗とは言えず、親repoの不存在とアクセス不足は区別できない。
実schema版・制約・正式fixtureを確認できていないので、上記初期化を実施済みとは報告しない。
親DDLを取得できるまで7件は **実結合未検証 / Hold**。最小schemaの推測やsyntheticな運用判定で代用しない。
