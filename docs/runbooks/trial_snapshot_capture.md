# ADR-002: 観測した試行台帳のスナップショット保存

`scripts/postgres/capture_trial_snapshot.py` は、実際に読み取った batch ID 全件・N・SR 十分統計量・
family 一覧・頻度フィルタ・snapshot_hash を append-only に保存し、保存 ID から再取得する監査用 CLI。
既存の DSR・FROST・昇格判定・台帳読取り API には配線していない。新しい判定に過去の保存値を自動適用しない。

## 意味と保証範囲

`recorded_at < as_of < commit` の batch は、通常の台帳読取りではコミット前後で見え方が変わる。
そこで、初回に観測した結果を独立した `qed_trial_snapshot_captures` に保存する。
同じ family・as-of・SR 頻度・祖先を含めるかの条件で再取得すると、最初に保存された値を返す。
未コミットの書込みが後から可視になっても、保存した N・batch ID・統計量・ハッシュは変えない。

これは**保存時に観測した状態**を再現する仕組みであり、任意の過去日時のコミット済み履歴を復元する機能ではない。
保存前の観測状態は復元できない。既存 `fetch_trial_snapshot(..., as_of=...)` の結果が後から変化する制約も残る。
未来の as-of は DB 時計で拒否する。最新状態を別途保存する際は、新しい as-of を明示して取得する。
`captured_at` は保存処理中の DB 時刻であり、元 batch のコミット時刻や可視性の境界ではない。

## 前提

Python 3、psycopg3、テスト済み PostgreSQL、migration 084 の後に 087 を適用する。
手動試行登録 CLI を併用する場合は 086 も適用する。既存テーブルの変更はない。
`QED_PG_DSN` は安全な環境設定で指定する。接続文字列をコマンド引数・記録ファイルに埋め込まない。
保存には新規テーブルへの SELECT / INSERT、台帳への SELECT 権限が必要。

## 保存と再取得

```bash
# family key は登録 CLI の出力等から取得する。
# as-of は過去または現在の、タイムゾーン付きの明示的な日時を指定する。
python scripts/postgres/capture_trial_snapshot.py capture \
  --family-key fam:0123456789abcdef0123456789abcdef \
  --as-of 2026-10-17T04:00:00Z --sr-periodicity daily

# 返された capture_id をそのまま指定する。
python scripts/postgres/capture_trial_snapshot.py show \
  --capture-id 'capture:<returned-64-hex-digest>'
```

`capture` は DB に保存する操作。同じ条件での再実行は既存の保存結果を返す。
`show` は保存済みの値だけを返し、台帳を再集計しない。存在しない ID、必要な migration の不足、
ハッシュ不一致は非ゼロ終了し、現在の台帳の値に差し替えて成功扱いしない。
`--direct-only` は祖先 family を含めない条件。`--sr-periodicity` を省略すると既存読取 API の頻度未指定と同じ集計を保存する。
頻度指定時の N と SR の扱いも既存 API をそのまま使う。

出力には以下を含む。元の証拠ファイルや DSN は保存・出力しない。

- `capture_id`: 読取条件から決まる ID。同じ DB 内で、その条件の最初の保存結果を指す。
- `snapshot`: batch ID 全件、family 一覧、N、SR 十分統計量、頻度、除外件数、既存 snapshot_hash。
- `payload_sha256`: バージョン・祖先設定・snapshot 全体を結び付ける SHA-256。
  既存 snapshot_hash だけでは含まれない N・SR 統計量も対象とし、再取得時に検証する。
- `captured_at`: 保存処理中の DB 壁時計。

日時は UTC に正規化する。同じ瞬間を `+09:00` と `Z` で指定しても同じ ID になる。
祖先設定・頻度フィルタが違う場合は別の保存条件となる。

## Python API とトランザクション

`analytics.python.pg_io.postgres_trial_snapshot_capture` の `capture_trial_snapshot` は、
idle な autocommit 接続だけを受け付け、自分で開始した REPEATABLE READ トランザクション内で
複数の台帳問合せと保存を行う。呼出し元の進行中トランザクションを commit / rollback しない。
既存の `register_manual_trials` は従来どおり呼出し元が外側のトランザクションを所有する。

同じ条件の初回保存が競合した場合、主キー制約と新規トランザクションでの再試行により、
成功した保存結果を共有する。台帳書込みのコミット待ちで読取りをブロックする方式にはしていない。
`get_snapshot_capture` は保存 ID を受け取り、保存値だけを検証・復元する。

## 検証

```bash
python -m pytest tests/unit/test_trial_snapshot_capture.py
# QED_PG_DSN はテスト専用 DB に設定
python -m pytest tests/integration/test_trial_snapshot_capture_pg.py
```

長時間書込みの commit / rollback、同時初回保存、読取り途中の commit、祖先と頻度フィルタ、
タイムゾーン、append-only、破損拒否、失敗後の再実行、呼出し元トランザクション保護、CLI を検証する。
変更前後の結果は [検証記録](../reports/adr002_snapshot_capture_validation_2026-10-17.md) を参照。
