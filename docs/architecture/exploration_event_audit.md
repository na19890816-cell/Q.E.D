# EML / FROST の探索イベント監査

2026-10-17 / Phase 5R Task D。main `a3e6390` を基準とする独立変更。
Task C の受入保存にも PR #1〜#4 のコードにも依存しない。
この機能はローカル POSIX のソフトウェア監査であり、実履歴や統計的アルファの検証ではない。

## 目的と境界

既存 EML は top_k 前の木をメモリで評価し、最後に SearchStats を返す。
FROST の batch 評価は途中例外で先行する成功結果も output から失う。
メタ検証の重み摂動は個別設定・順位を保存せず分布へ集約する。
これらの新規実行を明示的に監査するため、開始前に設定を固定し、評価前に開始イベントを
fsync し、結果・失敗・非有限値と正常終了時の件数を別イベントに固定する。

有効化は Python API の `audit=` 指定だけ。環境変数から自動有効化しない。
`run_eml_discovery`、`exhaustive_search`、`gradient_search`、
`evaluate_candidates_batch`、`run_frost_pipeline`、`run_meta_validation` とその設定比較関数が対応する。
既存の default 呼出し、DSR/PBO、スコア、Hard Gate、昇格条件は変更しない。
監査 sink は DB・ネットワーク・cron・売買に接続しない。
既存 `run_frost_pipeline` 自体の DB writer は従来どおりであり、本機能はそれを無効化する
オフラインフラグではない。今回のテストでは writer を明示的に stub 化し DB 接続しない。

未指定の入口、外部 callback 内で独自実施した探索、既存 worker の直接呼出しは完全性の保証外。
新しい研究では開始から同じ audit を伝播し、終了時に `finish()` と独立した再読取を必須とする。
途中から有効化した記録を全探索履歴と呼ばない。

## 不変の識別・証拠

`ExplorationAudit.create(root, ...)` に以下を必須指定する。

| 項目 | 意味 |
|---|---|
| `search_id` | 実行 attempt の一意な ID。同一 ID で再計算・上書きしない |
| `run_id` | ADR-002 の run。独立再評価には新しい run を付与する |
| `family_key` | 明示した研究 family (`fam:` + 32 hex) |
| `policy_hash` | 生産者が宣言した policy の SHA-256。FROST runner は実 PolicySpec と照合 |
| `code_commit` | 40 桁の Git commit。実行環境・未コミット差分の証明は別途必要 |
| `input_evidence_sha256` | 実際に使用する入力証拠の hash。原データの保全先は別途必要 |
| `settings` | 開始時点の候補・重み・閾値・seed 等の宣言。認証情報を含めない |
| `historical_n_raw` | 根拠付き既知値、または `null`。未知を 0/1 で埋めない |

context と設定は開始イベントに保存する。入力 hash、commit、policy、過去 N は呼出側の
宣言であり、Git checkout・データ完全性・台帳保存成功を自動証明しない。
EML discovery は実効 `EML_PROMO_W_*` も開始設定へ保存し、参照評価前に変更があれば停止する。
他スレッドが計算中に環境変数を変更する実行を許可しない。
FROST 各候補は独自 family と既存既定値使用の有無、実効 lineage 既定設定を記録する。
family の既定補充は既存 S2 の帰属を説明する記録で、上流 provenance の解決を意味しない。

## 保存プロトコル

root とその祖先は事前に永続 provision された既存ディレクトリを指定する。
API は祖先ディレクトリを再帰作成しない。
root 以下に `sha256(search_id)/` を排他的に作る。同 ID は内容一致でも新規実行を拒否する。
root の registry lock 下で run_id の予約ファイルも排他的に fsync 保存し、
同じ root 内で別 search に同 run_id を再使用する再計算を拒否する。
同じ audit object を EML → FROST 等の stage に伝播することは可能。
別 root、既存の非監査 run、他のシステムとの run 衝突検知は保証しない。
既存記録の再取得は `read_events` / `inspect_attempt` で行い、再計算を冪等な再送と呼ばない。
同一 attempt は単一 producer が使用する。複数の独立 attempt と同一 ID の同時作成に対応し、
同一 ID は一つだけ成功する。プロセス間で一つの writer instance を共有しない。

各イベントは `000000000001.json` から連番の独立ファイルとなる。

1. `flock` で publish を直列化する。
2. 同じディレクトリの `.pending-*` に strict JSON bytes を書く。
3. flush、mode 0400、ファイル fsync を完了する。
4. `link` で新しい連番名へ排他的に publish する。既存ファイルを置換しない。
5. ディレクトリ fsync 後だけ成功を返す。一時名を除去する。

各イベントは `schema_version=1`、sequence、previous_sha256、context、event_type、
実際の UTC `recorded_at`、data、全体の `event_sha256` を持つ。
data 内の evidence/result も独立 hash で固定する。UTC は監査側の実観測時刻で、過去の
available_at や実験開始前に生成された物の正確な birth time を推測しない。
exhaustive の全木列挙開始前には scope を保存するが、列挙器内部で各木が作られた時刻は
取得できない。各木の生成観測・評価開始は列挙完了後に保存する。

イベントは `RUN_STARTED → SCOPE_STARTED → TRIAL_STARTED → ... → TRIAL_FINISHED
→ SCOPE_FINISHED → RUN_FINISHED`。開始時の計画数と終了時の実測数を上書き統合しない。
optimizer probe は別 EVALUATION イベントとして raw weight/tree、順序、結果を保存する。
最終 snap 後の式評価だけを S1 の count と照合する。
top_k 外・重複除去・安全式拒否・閾値判断は disposition に残す。
FROST は最初の評価前に投入された全候補を登録し、途中失敗後の未訪問候補も識別できる。
閾値・軸除去・重み摂動は設定ごとの評価前 start と個別 snapshot/metrics を残す。

NaN/±Inf は `{"$nonfinite":"nan|+inf|-inf"}` で区別し、None は null のまま保存する。
失敗は FAILED、非有限結果は NONFINITE、未評価の depth 違反は INVALID。
既存計算が返す sentinel や sanitized result は変更せず、監査状態を分離する。
例外は型名だけを記録し、任意の例外メッセージ・stack trace・DSN を保存しない。
設定の credential-like key を拒否する。任意の文字列全ての秘密を自動識別する仕組みではない。

## ADR-002 との整合

イベントは追加の N 供給元ではない。台帳への書込・自動 export は一切しない。

| 経路 | 計上の所有元と規則 |
|---|---|
| EML exhaustive / gradient | S1。stage/run ごとの異なる最終 compiled_expr。stage 横断・独立 run 横断は別計上 |
| Adam finite differences | optimizer の参照評価。追加の独立 N にしない |
| EML full evaluation / promotion threshold | 探索済み式の参照。S1 証拠確認が別途必要 |
| EML → FROST | S2 の既存除外規則を記録。S1 保存済みとは断言しない |
| 非 EML → FROST | S2。既存の family/run ごとの distinct candidate_hash、空 hash は candidate_id |
| 外部 / 手動 | S3 の別契約。audit を再投入して同一試行を二重登録しない |
| 重み・閾値・family の研究設定 | RESEARCH_ONLY。全設定を保存するが、N への帰属・独立性は人間が ADR に従って判断 |

`ledger_batch_id=null`、`ledger_write_verified=false` を明示する。
084 未適用・lineage 無効・書込失敗・過去欠落を journal が補償したとは扱わない。
既存 S1/S2 は同 run の batch ID 衝突を DO NOTHING にするため、別設定の独立再評価に
同 run_id を再使用しない。audit は search_id 単位の別記録を保持しても、この台帳制約を
解消しない。S3 と S1/S2 の cross-source 完全照合は未実装。
過去の N と未完了探索の最終 N は未知のまま維持し、採用評価を Hold とする。
family 変更は別 run/search とし、`settings.parent_family_key`、`parent_search_id`、
`change_kind="family_derivation"` に実際の派生元を明示する。イベントだけで系譜 edge を
作ったとは扱わない。既存 ADR-002 の edge と祖先 N の帰属確認は別作業であり、
単なる renamed family への移動で過去 N をリセットしてよいという契約ではない。

## 再起動・障害と保証範囲

`inspect_attempt` は hash chain、連番、context を再検証し、未完了 trial ID、
scope 統計、orphan staging 数、最後の event hash を返す。
read/inspect は検証した event 全ファイル・attempt directory・root を再 fsync してから
成功を返す。link 後の directory fsync 失敗で残ったファイルにも耐久性を再確立する。
再 fsync が失敗すれば可視なファイルでも成功と ACK しない。
CLI: `python scripts/audit/inspect_exploration.py <attempt-directory>`。
完了は exit 0、整合している未完了は exit 1、破損は exit 2。
fsync/容量不足/競合等の監査障害では次の評価を開始しない。
原子 publish 前の書込断は未公開 temp、後の断は全 JSON のどちらかとなる。
ファイルは完了していても acknowledgement が失われる可能性があるため、再起動時は
検証済み原本を読み、同じ ID の再実行や自動上書きはしない。

計算の途中再開・未完了 run の自動修復・新規実行との自動 N 統合は未対応。
run の予約後、開始イベント保存前の障害もあり得る。予約を自動解除して再計算せず、
未完了として調査する。backup は run 予約を含む root 全体を保存する。
再実験には新 run/search を付け、前 attempt の最後の hash を settings へ参照として残す。
原入力は別途永続化し、回復用バックアップ・外部 checkpoint を用意する必要がある。
journaling 非対応 FS、NFS/object storage、ディスクが fsync を偽る障害、OS/所有者による
意図的変更、全末尾削除、完全な chain 再生成にはこの実装だけで耐えない。
追記時には既存全ファイルを再読しないため、途中の bit rot は最終/独立 inspect 時に検出する。
append-only は本 API と通常の読み取り専用 mode による保証で、WORM/外部署名ではない。

資源はイベント数に比例する。候補・probe ごとの file と fsync があり、大規模 EML では
inode と IOPS が律速となる。1 event の平均 serialized bytes を B、イベント数を E、
FS block を K とすると目安は `E * ceil(B/K) * K` + directory/backup、inode は約 E。
gradient は `2 * steps * EML-node-count * n_init` の probe があり、N_raw とは別である。
実 run 前に容量・inode・fsync latency を計測し、永続ディスクと backup 容量を確保する。
原市場データや returns の量は取得先未確定のため未算定。

## 実行例

```python
audit = ExplorationAudit.create(
    audit_root, search_id=new_attempt_id, run_id=config.run_id,
    family_key=explicit_family_key, policy_hash=declared_policy_hash,
    code_commit=full_git_commit, input_evidence_sha256=actual_input_hash,
    settings={"experiment_plan_sha256": frozen_plan_hash}, historical_n_raw=None,
)
output = run_eml_discovery(config, feature_df, target, audit=audit)
receipt = audit.finish()
assert receipt["state"] == "COMPLETED"
# Store last_event_sha256 outside this directory for stronger deletion detection.
```

値は全て実際の入力から用意し、不明値の埋合せ例として使わない。
FROST の単体 batch 評価と meta 検証も同様に audit を伝播できる。
既存本番 runner/cron への rollout は本変更の範囲外。
