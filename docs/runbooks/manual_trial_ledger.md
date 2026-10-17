# ADR-002 S3: 手動・外部アルファの試行数登録

既存の DSR・FROST・昇格判定は変更しない。登録は観測・記録のみで、昇格を実行しない。
ADR-002 の承認ステータスも変更しない。

## 前提

Python 3、psycopg3、PostgreSQL、migration 084 と 086 が必要。
既存のマイグレーション手順に従い、084 の後に
`qedschema/migrations/086_qed_manual_trial_imports.sql` を適用する。
`QED_PG_DSN` は安全な環境設定から渡し、引数・証拠 JSON に認証情報を含めない。

## 証拠ファイル

次の JSON（UTF-8）の `trials` は、当該研究 run で評価した**全て**の式を含める。
採用済み・公開済みのアルファだけの一覧は禁止。棄却した候補や未保存の候補も必要。

```json
{
  "version": 1,
  "source": "research-desk",
  "source_type": "external",
  "research_run_id": "external-study-2026-10-16-01",
  "complete": true,
  "n_trials": 2,
  "completed_at": "2026-10-16T12:00:00Z",
  "family": {
    "horizon": "5d",
    "universe": "event_study_panel",
    "target": "abnormal_return",
    "terminal_set_hash": ""
  },
  "trials": [
    {"formula": "r1 + r5", "evaluated_at": "2026-10-16T11:00:00Z", "data_cutoff": "2026-10-15T00:00:00Z"},
    {"formula": "r20", "evaluated_at": "2026-10-16T11:00:00Z", "data_cutoff": "2026-10-15T00:00:00Z"}
  ]
}
```

- `source_type` は `manual` または `external`。いずれも台帳の stage は `manual`。
- `source × research_run_id` はプロバイダの元の研究 run を識別する不変のキー。
  再送ごとに新しい ID を発行しない。`submitted_by` は CLI で指定する登録担当者。
- family は S1/S2 と同じ horizon × universe × target × terminal_set_hash から計算する。
  family の意図的な細分化で N を減らしてはならない。
- N は ADR-002 の定義どおり、空白正規化した異なる式の数。同じ式を再評価した行があっても当該 batch 内では 1 と数える。
  CLI の N、証拠内の N、検算値は完全一致が必要（正の PostgreSQL INTEGER）。
- 時刻にはタイムゾーン必須。各行で `data_cutoff <= evaluated_at <= completed_at` が必要。
  書き込み時には `completed_at <= DB clock_timestamp()` も確認する。
- 不明なキー、重複する JSON キー、日時・型の不正、集約件数のみの証拠は拒否する。
  SR やスコアは受け付けず、選択後標本からの SR 分散を DSR へ混入させない。

## 実行

リポジトリルートで、利用中の Python 環境を有効化して実行する。

```bash
# DB に接続せず、証拠・N の整合性とハッシュを確認
python scripts/postgres/register_manual_trials.py \
  --evidence /path/to/evidence.json --n-trials 2 --submitted-by researcher

# QED_PG_DSN の DB に登録（コミットまで実施）
python scripts/postgres/register_manual_trials.py \
  --evidence /path/to/evidence.json --n-trials 2 --submitted-by researcher --write-db
```

検証のみの場合は `status=validated_only`。未来時刻と DB の照合は書き込み時に行う。
登録時は `inserted`、同一キー・同一証拠の再実行は `already_registered`。
標準出力の JSON は batch_id / family_key / N / 2 種の SHA-256 と DB 登録時刻を返す。
失敗は非ゼロ終了する。台帳未作成や DB 接続失敗を成功扱いしない。

## 冪等性・監査

同一キーの N・family・証拠変更は拒否し、元の登録を保持する。異なるキーでも同じ証拠、
ラベルを変えただけの同じ一覧、整形・並べ替え・式の空白変更・完全に同じ行の追加による再登録は拒否する。
同時登録でも UNIQUE 制約とトランザクションにより batch だけが残ることはない。
完全一致の再送以外は、監査のため元ファイルのバイト列を維持する。

原ファイルの SHA-256 と正規化した一覧の SHA-256 は
`qed_trial_batches.metadata` と `qed_manual_trial_imports` に保存する。
一覧ハッシュは `inventory_hash_version=2` とし、family と、式・評価時刻・データ締切を正規化した
試行行の集合から計算する。提出全体の `completed_at`、source/run ID、担当者は含めない。
同じ一覧の完了時刻や ID を変えても新しい試行にはならない。
`completed_at` は原証拠の SHA-256、metadata、receipt に引き続き保存・検証するため、
同じ ID の完了時刻差し替えも拒否する。試行行の評価時刻が異なる独立評価は従来どおり別に計上する。
原ファイル自体は DB に複製しないため、提出元の完全なログと共にアクセス制御された場所に保管する。
receipt も batch も append-only。修正は上書きではなく、原因を調査して別途監査手順を決める。

この Draft の旧版（`95ecf78`）で試用した receipt は完了時刻を含む旧ハッシュであり、
ハッシュだけから新形式に変換できない。新 CLI は旧形式の receipt がある family の登録・再送を拒否する。
旧 CLI を停止し、新 CLI に統一する。旧データは原証拠を復元して重複監査・移行方針を決めるまで保持し、
自動再ハッシュ・UPDATE・DELETE で帳尻を合わせない。新旧 CLI の混在運用はサポートしない。

`recorded_at` は DB 壁時計で決定し、証拠内の完了時刻やトランザクション開始時刻では決めない。
`as_of < recorded_at` の snapshot に後から入ることはない。ただし `recorded_at` はコミット時刻ではない。
登録後・コミット前の時刻を as-of とする読取りは、コミット前には N=0、コミット後には N=2 に変わり得る。
READ COMMITTED では再問合せごとに変わり、REPEATABLE READ でも新トランザクションでは変わる。
短いトランザクションや CLI の即時コミットは、この時刻境界の不変性を保証しない。
固定した壁時計日時と snapshot_hash だけでは当時の可視集合を復元できないため、監査には実際に参照した
batch ID 全件と集計入力を保存する必要がある。現在の CLI はその監査保存を実装していない。
コミット可視性を基準とする履歴の固定は未実装であり、既存 DSR・FROST・昇格ロジックと読取り API は変更していない。
再現テストと結果は [PR #1 追加レビュー記録](../reports/adr002_s3_pr1_review_2026-10-17.md) を参照。

S1/S2 で登録した同一評価の再梱包には本 CLI を使わない。独立した再評価や別 stage の N_raw 加算規則は変更しない。
上流が隠した試行・虚偽の出典や時刻はハッシュだけで証明できない。
`complete=true` は生産者の宣言であり、実運用では元の探索ログとの照合が必要。
この CLI は提出証拠の一貫性・重複・登録時刻を保証し、アルファ式そのものの経済的ルックアヘッドを検証するものではない。

## テスト

```bash
python -m pytest tests/unit/test_adr002_s3_manual_ledger.py
# QED_PG_DSN はテスト専用 DB を設定（実行ごとに一意な family を使用）
python -m pytest tests/integration/test_adr002_s3_manual_pg.py
```

実 DB テストは migration 084/086 の二重適用、完全一致再送、差し替え拒否、ロールバック、
同時登録、append-only、as-of、CLI を通した登録・再登録を確認する。
