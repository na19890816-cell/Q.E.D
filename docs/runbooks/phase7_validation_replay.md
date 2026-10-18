# Phase 7 の隔離再検証

実 EC2/DB/cron に接続しない。既存の環境設定・認証情報を表示しない。
root の親はローカルで provision した writable/durable directory を用意する。

## 対象試験と raw→receipt

新 branch を専用 checkout に取得し、リポジトリ root を cwd とする。
Python 3.12.14、pytest 9.1.1、numpy 2.5.4、pandas 3.0.6、psycopg 3.3.6、jsonschema 4.25.1
を使用した。依存版は報告 JSON に固定。以下の `<...>` は実環境の安全な場所に置換する。

```sh
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN <python> -m pytest -q \
  tests/unit/test_raw_json_artifact.py \
  tests/integration/test_raw_json_artifact_store.py \
  tests/unit/test_local_ingestion.py \
  tests/unit/test_ingestion_manifest_profile.py \
  tests/integration/test_local_ingestion_recovery.py \
  tests/unit/test_prostock2_contract.py \
  tests/integration/test_prostock2_contract_pipeline.py \
  --junitxml=<new-evidence-dir>/targeted.xml
<python> scripts/audit/phase7_artifact_roundtrip.py --work-dir <new-local-directory>
```

対象351成功。roundtrip は合成専用で、以前の directory があれば上書きせず停止する。
synthetic-original.json、producer-complete-manifest.json、receiver の raw/intents/receipts と
roundtrip-result.json を照合する。raw SHA は manifest canonical SHA と異なる。

## 全体の前後比較

`git worktree add --detach <new-baseline> 35d670f7181c870cfffd1a5da037e6ad41b66796`
で PR #5 HEAD、別 worktree で `a7490fcbbe0d5721bf8fa25ef0dfde1131e832d8` を checkout。
それぞれ cwd/PYTHONPATH を当該 tree に設定し、同一 interpreter で DB 環境変数を除去して
`python -m pytest -q --junitxml=<別々のfile>` を実行する。
基準1876成功/3失敗/70skip、追加後1957/3/70。既存固定パス3失敗を隠さない。

統合は main `a3e63907c2f529f289101698ee7d62f8bf72f436` から**新しい使い捨て branch/worktree**を
作り、次の固定 commit を `git -c commit.gpgsign=false merge --no-ff --no-edit <sha>` で順番に
ローカル統合する。main/既存 branch を checkout/書換え/push しない。

| 順序 | PR | commit |
|---:|---:|---|
| 1 | 2 | 99604b427716a760a949d85b311f4ecb9728804c |
| 2 | 1 | fbb0ef26a5a5f889dedf9aeed8d858635da68aa0 |
| 3 | 3 | 44c974b585f292e91d62de3f7bb8b7b3a3343c11 |
| 4 | 4 | c92cef13ed01542145c2e97989efce88f9bd64e5 |
| 5 | 5 | 35d670f7181c870cfffd1a5da037e6ad41b66796 |
| 6 | 6 | 3bca51dc11779ef51416888e111c620d01cdceb3 |
| 7 | 7 | 54061f4c4bd3c7b67e46387478a283d7f244c85a |
| 8 | 本実装 | a7490fcbbe0d5721bf8fa25ef0dfde1131e832d8 |

commit 未取得なら read-only fetch で取得し、固定 SHA と照合する。merge conflict が出た場合は
その隔離 branch で止めて報告する。既存 PR の base/履歴を変更しない。
順序7時点と8時点で同じ全体 no-DB command を実行し、2068/0/156 → 2149/0/156 を比較する。
Junit の classname::name を ID とし、新規失敗/追加skip/削除ID/重複ID が0であることを確認する。
新規81件を既存skipに混ぜない。PG 試験・外部7件の成功をこの結果から推定しない。

前向き監査 probe は PR #6 を含む tree を指定する。

```sh
<python> scripts/audit/phase7_prospective_audit_probe.py --tree <combined-tree>
```

合成 registry の欠落/監査未指定/書込失敗で callback が呼ばれないことまで。
本番 registry・認証・off-host・全体監査保証を意味しない。

## 保存証跡の確認

`docs/reports/evidence/phase7_2026-10-18.tar.gz` をそのまま SHA-256 検証し、
`phase7_evidence_manifest_2026-10-18.json` の全 file hash/size を照合する。
展開は新しい制限 directory に行う。生 XML/log と roundtrip 原本/manifest/receipt は全て合成。
秘密情報の pattern 検索は補助で、実原本の公開承認の代わりにはならない。
