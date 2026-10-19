# Phase 8C の前後回帰再現

DB/EC2/cron/売買へ接続しない。scratch は新しい隔離 worktree と directory を使用する。
正式原本は未提供。ここで再現する結果は合成ソフトウェア品質のみ。

## 同じ依存環境

Python 3.12.14、pytest 9.1.1、numpy 2.5.4、pandas 3.0.6、psycopg 3.3.6、jsonschema 4.25.1、
system IANA tzdata 2026b。新しい third-party 依存はない。timezone database がなければ
推測に fallback せずエラー。依存版は validation JSON に記録。

PR #8 HEAD `2d2d5dc1b7c4cb199a7cf63157641c0cc4ee498a` と、本実装
`b3f062608ac283fc3abf734e9296184961ec9edc` を別 worktree に取得する。
cwd と PYTHONPATH を各 tree に設定し、同じ interpreter で次を実行する。
`<...>` は新しい安全な保存先に置換し、秘密環境変数を表示しない。

```sh
env -u QED_PG_DSN -u QED_MIGRATION_TEST_DSN <python> -m pytest -q --junitxml=<new-evidence-dir>/result.xml
```

PR #8基準1957/3/70 → 本実装2037/3/70（成功/失敗/skip）。exit1 を成功と呼ばない。
固定パス依存の既存3件は報告に完全IDを列挙。既存テストのskip/xfail化やbaseline再生成をしない。

対象試験は以下9fileを同じ command に指定し、431成功/0失敗/0skipを確認する。

```
tests/unit/test_identity_pit_audit.py
tests/integration/test_identity_artifact_audit.py
tests/unit/test_raw_json_artifact.py
tests/integration/test_raw_json_artifact_store.py
tests/unit/test_local_ingestion.py
tests/unit/test_ingestion_manifest_profile.py
tests/integration/test_local_ingestion_recovery.py
tests/unit/test_prostock2_contract.py
tests/integration/test_prostock2_contract_pipeline.py
```

## 全 PR を組み込む隔離検証

main `a3e63907c2f529f289101698ee7d62f8bf72f436` から新しい使い捨て branch/worktree を作り、
以下を `git -c commit.gpgsign=false merge --no-ff --no-edit <sha>` でローカル統合する。
commit 未取得なら read-only fetch で取得して固定 SHA を確認。main/既存 PR branch は書き換えない。

| 順序 | PR | SHA |
|---:|---:|---|
| 1 | 2 | 99604b427716a760a949d85b311f4ecb9728804c |
| 2 | 1 | fbb0ef26a5a5f889dedf9aeed8d858635da68aa0 |
| 3 | 3 | 44c974b585f292e91d62de3f7bb8b7b3a3343c11 |
| 4 | 4 | c92cef13ed01542145c2e97989efce88f9bd64e5 |
| 5 | 5 | 35d670f7181c870cfffd1a5da037e6ad41b66796 |
| 6 | 6 | 3bca51dc11779ef51416888e111c620d01cdceb3 |
| 7 | 7 | 54061f4c4bd3c7b67e46387478a283d7f244c85a |
| 8 | 8 | 2d2d5dc1b7c4cb199a7cf63157641c0cc4ee498a |
| 9 | 本実装 | b3f062608ac283fc3abf734e9296184961ec9edc |

8時点と9時点で同じ no-DB 全体試験を実行する。2149/0/156 → 2229/0/156。
今回の tree SHA は追加前 `bdfcfa2bd616de3f15db27857951dd1c4489752f`、
追加後 `774a5cc34964f21fdf7fa45cf8075e680b28400f`。
merge metadata の日時/authorが異なる再現では commit SHA が変わるため tree で比較する。
conflict時は隔離 branchで停止・報告し、既存PRのbaseや履歴を変更しない。

JUnit の classname::name を ID とし、新規失敗・追加skip・削除ID・重複ID が0か照合する。
新規80件が全て実行され、skip/xfailがないことを確認する。DBのskip156件を成功に加算せず、
正式親スキーマ7件や過去のPG成功を今回の成功と読み替えない。

## 原本→receipt→監査分類

```sh
<python> scripts/audit/phase8c_identity_pit_probe.py --work-dir <new-local-directory>
```

10の合成シナリオと明示監査事実を再生成する。実 Producer の構造・code/name・利用可能性を
証明するものではない。元 raw/manifest/receipt と派生report/input_facts_sha256 を照合し、
研究前preflightの9遮断と、対照1件でも research_eligible=false を確認する。
新 directoryのみを受け付け、既存成果物を上書きしない。
evidence archive の SHA と manifest 全 file hash/size を確認してから限定 directoryへ展開する。
