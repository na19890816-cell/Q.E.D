# 固定スナップショット保存の追加レビュー — 2026-10-17

比較基準は PR #1 の `cbff751`。main は `a3e6390` を維持し、更新・マージしない。
前回と同じ Python 3.12 / PostgreSQL 17 / psycopg 3.3.6 のローカルテスト専用 DB で検証した。

## 再現した問題と修正

### SQL_ASCII 接続

正常なキャプチャを UTF8 接続で保存後、同じ接続の client_encoding を SQL_ASCII にして再取得した。
PostgreSQL の TEXT 列が bytes で返るため、文字列の SHA-256 と bytes の保存ハッシュの比較が不一致になり、
正しいデータでも `snapshot capture payload checksum mismatch` になった。
SQL_ASCII での新規保存も同じ理由で失敗した。

既存 live 読取層と同じ TEXT デコードを使い、capture ID と保存ハッシュを文字列に正規化してから検証する。
新規保存・同条件の再取得・ID 指定再取得と JSON 出力を、Unicode を含む元台帳データで確認した。
テスト終了時には元の接続エンコーディングを復元する。保存形式とハッシュの定義は変更しない。

### TRUNCATE による履歴削除

UPDATE / DELETE の拒否トリガは TRUNCATE には適用されない。
修正前は `TRUNCATE qed_trial_snapshot_captures` が成功し、同じトランザクション内の保存行数は 0 になった。
再現確認は意図的に rollback し、保存済みデータの行数が元と一致することを確認した。実際の履歴は削除していない。

migration 087 に BEFORE TRUNCATE の文レベルトリガを追加し、既存の append-only 拒否関数を使う。
本 PR 以前の既存テーブル・関数や判定コードは変更しない。087 を再適用すれば、旧 Draft で作成したテーブルにもトリガを追加できる。
二重適用とデータ保持を確認する。新しい回帰テストは、もし脆弱な版で TRUNCATE が成功しても
必ず例外でトランザクションを rollback し、テストでアーカイブを消さない作りにした。

## テスト結果

新しい回帰ケースを修正前に実行し、**2 failed / 89 passed** を確認した。
修正後は固定保存の単体 11 件・PostgreSQL 18 件、既存 S3 の単体 40 件・PostgreSQL 22 件を合わせて **91 passed**。

| 検証 | 変更前 `cbff751` | 修正後 |
|---|---|---|
| DB 接続変数なし `tests/` | 1659 passed / 3 failed / 108 skipped | 1659 passed / 3 failed / 110 skipped |
| 実 PostgreSQL `tests/integration/` | 99 passed / 7 failed / 0 skipped | 101 passed / 7 failed / 0 skipped |
| 固定保存 + S3 の対象テスト | 89 passed | 91 passed |

JUnit の失敗 ID は、DB なし 3 件・実 DB 7 件とも前後で完全一致した。新規失敗はない。
追加 2 件はいずれも PostgreSQL テストのため、DB なしではスキップが 2 件増加し、実 DB では成功が 2 件増加した。
全体の終了コードは前後とも **1** であり、全件成功ではない。既存 3 件は `/home/user/prostock` 固定パス、
既存 7 件は外部の `evidence_bundles` / `factor_candidates` 不足による。
全 migration の二重適用も成功し、旧データがあるテーブルへの TRUNCATE トリガ追加を検証した。
`git diff --check` を通過し、既存の判定・live 読取・S3 登録コードが未変更であることも差分で確認した。

比較元の全体結果は、同じ環境で既に取得済みの `cbff751` の [初期検証](adr002_snapshot_capture_validation_2026-10-17.md) を再利用する。
変更後は全体スイートを再実行し、JUnit から失敗 ID の集合を比較する。
コマンド・接続設定は初期検証と同じ。対象テストは以下で再現できる。

```bash
# QED_PG_DSN はテスト専用 DB に設定する
.venv/bin/python -m pytest tests/unit/test_trial_snapshot_capture.py tests/integration/test_trial_snapshot_capture_pg.py tests/unit/test_adr002_s3_manual_ledger.py tests/integration/test_adr002_s3_manual_pg.py -q -o addopts= --tb=short
```

再現結果 JSON、修正前後の対象ログ・JUnit、変更後の全体結果は `/workspace/work/qed-pr1-review/hardening-*` に保存した。

## 変更の範囲

実装変更は、この PR で追加した固定保存のデコードと migration 087 の TRUNCATE トリガのみ。
既存 DSR・FROST・昇格ロジック・live 読取・S3 登録は変更しない。
保存済みの観測結果の再現性を保護する修正であり、任意の過去日時のコミット済み集合を復元する機能は追加していない。
