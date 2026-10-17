# 台帳 live 読取りの整合性レビュー — 2026-10-17

比較基準は PR #1 の `d771747`。main は `a3e6390` を維持し、マージしない。
Python 3.12 / PostgreSQL 17 / psycopg 3.3.6 のローカルテスト専用 DB で検証した。

## 1. 複数問合せの間のコミットで、祖先と batch が食い違う

修正前の `load_ledger_for_family` は、祖先 family → edge → batch の 3 文を実行する。
READ COMMITTED では各文で可視性が変わるため、次の不整合を再現した。

1. 親 P の batch N=100 と子 C の batch N=10 はコミット済み。P→C の edge は別接続 W で未コミット。
2. 読取 R の祖先問合せが完了して C だけを取得した直後、W をコミット。
3. 後続の edge 問合せは P→C を取得するが、batch 問合せは最初に得た C だけを対象にする。
4. 結果の family は P と C、採用 batch は C のみ、N=10 となる。
   コミット前の状態（family=C / N=10）とも、コミット後の状態（family=P,C / N=110）とも一致しない。

autocommit の有無を変えた 2 ケースで失敗を確認した。同期には cursor の最初の問合せ完了点を使い、sleep は使っていない。
回帰追加後・修正前の対象スイートは **2 failed / 204 passed**。

修正後は再帰 CTE と UNION ALL で、祖先・edge・batch を単一 SQL の同じ MVCC 可視性から読む。
最初の読取りは C / N=10 を返し、後の新しい読取りでは P,C / N=110 を返す。
`include_ancestors=False` は独立した batch の単一 SELECT を使い、edge テーブルへの権限・存在を新たに要求しない。
呼出し元の commit / rollback や分離レベル変更を行わず、未コミットの自己書込みも従来どおり読める。

## 2. naive as-of が SQL と Python で違う時刻になる

Python コアはタイムゾーンのない datetime を UTC として扱うが、SQL の timestamptz cast は接続 TimeZone を使っていた。
東京設定で `2026-01-02 00:00:00` を指定すると、SQL は UTC の 1 月 1 日 15:00 として祖先を枝刈りし、
Python は UTC の 1 月 2 日 00:00 として集計する。
その間の 1 月 1 日 18:00 UTC に記録した親 edge が欠け、正しい N=110 が N=10 になることを再現した。
追加ケースは修正前に **1 failed**。

SQL へ渡す cutoff に、コア既存の UTC 正規化関数を適用する。
コアの日時規則自体は変えず、東京接続でも naive と UTC aware の同値を確認した。
固定保存 CLI は引き続き明示的なタイムゾーンを要求し、入力仕様を緩めない。

## 互換性と回帰

- 循環を含む系譜、未来 edge、未来 batch、祖先有無、SR 頻度、cutoff 有無の **48 組合せ**で、
  DB の結果を純関数の TrialLedger と照合し、N・SR 統計・family・batch ID・hash が一致した。
- batch / edge の全フィールドを照合し、Unicode・JSON metadata・SR 精度・日時の round-trip を確認した。
  float8 と timestamptz は native 型で受け取り、JSON 経由で精度や日時の解釈を変えない。
- 呼出し元の未コミット書込みが他接続へ見えないまま読めること、分離レベル維持と caller rollback を確認した。
- edge テーブルがない一時 schema でも direct-only が動作することを確認した。
- 既存の SQL_ASCII・固定保存・S3 のテストを含む対象 **208 件が成功**。

| 検証 | 変更前 `d771747` | 最終修正後 |
|---|---|---|
| DB 接続変数なし `tests/` | 1659 passed / 3 failed / 110 skipped | 1659 passed / 3 failed / 116 skipped |
| 実 PostgreSQL `tests/integration/` | 101 passed / 7 failed / 0 skipped | 107 passed / 7 failed / 0 skipped |
| 台帳・固定保存・S3 の対象 | 既存ケースは全体測定で通過済み | 208 passed |

JUnit の失敗 ID は、DB なし 3 件・実 DB 7 件とも前後で完全一致した。新規失敗はない。
追加 6 件はすべて実 DB テストで、DB なしではスキップが 6 件、実 DB では成功が 6 件増加した。
既存 3 件は `/home/user/prostock` 固定パス、既存 7 件は外部 `evidence_bundles` / `factor_candidates` 不足による。
全体の終了コードは前後とも **1** で、全件成功とは扱わない。`git diff --check` を通過した。
direct-only の互換性確認を追加した後の最終実装で、対象 208 件と全体スイートを再実行した。

変更前の全体結果は、同じ環境で測定済みの `d771747` の [前回記録](adr002_snapshot_capture_hardening_2026-10-17.md) を再利用する。
変更後は全体スイートを再実行し、JUnit の失敗 ID を比較する。
ログ・JUnit・JSON は `/workspace/work/qed-pr1-review/atomic-read-*` に保存した。

```bash
# QED_PG_DSN はテスト専用 DB に設定
.venv/bin/python -m pytest tests/unit/test_adr002_lineage.py tests/integration/test_adr002_lineage_pg.py tests/unit/test_trial_snapshot_capture.py tests/integration/test_trial_snapshot_capture_pg.py tests/unit/test_adr002_s3_manual_ledger.py tests/integration/test_adr002_s3_manual_pg.py -q -o addopts= --tb=short
```

## 保証範囲と残る制約

今回は既存の台帳 DB 読取ブリッジを修正した。API の引数・戻り値・UTC の意味は維持し、
DSR・FROST・昇格条件・純関数の集計・書込み・既存スキーマは変更していない。新しい migration やサーバー設定は不要。

保証するのは **単一呼出し内の可視性の整合**。`recorded_at < as_of < commit` の未コミット行は、
別の呼出しではコミット後に見えるため、同じ as-of でも N=0→2 という変化は依然として起こる。
既存のその制約の再現テストも成功しており、履歴不変に修正済みとは扱わない。
現在の recorded_at だけから過去のコミット可視性は復元できない。
観測した入力を後から同じ内容で再現する用途は、既存の [固定保存](../runbooks/trial_snapshot_capture.md) を使う。
