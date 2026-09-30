# ADR-002: 系譜ログ — 試行台帳 (Trial Ledger) B 設計

**Status**: Proposed — 2026-09-30（Nao の承認待ち）
**Context**: NOTE-001 (DSR) の試行回数 N 集計基盤 / QED_REVIEW_2026-06-13 §3 ギャップ1
**関連**: ADR-001 (numpy ポリシー), NOTE-001, PolicySpec (Phase 1)

> **注記（再構成について）**: QED_REVIEW_2026-06-13 が参照する「ADR-002 ドラフト（B 設計 / 第2部 AlphaEvo 比較）」の
> 原文はリポジトリにも uploaded_files にも存在しない。本 ADR はレビュー・NOTE-001・リファクタリング計画における
> 参照箇所から要件を逆算して再構成したものである。原ドラフトが存在する場合は §9 の照合項目に従って統合すること。
> 第2部（AlphaEvo 比較）の凍結観点は NOTE-004 / NOTE-005 として既に登録済みのため本 ADR では扱わない。

---

## 1. 問題

DSR（NOTE-001）は「そのアルファに到達するまでに探索した試行数 N」と「試行間の SR 分散 V[SR]」を入力に取る。
現行スキーマでは run を横断してこれを集計できない。調査で判明した具体的な欠落は次の 3 点。

| # | 欠落 | 所在 | 影響 |
|---|------|------|------|
| L1 | **評価したが保存しない試行が消える** | `eml_search.exhaustive_search` は全木を評価し `top_k` のみ返す。`eml_master_formula` の `total_searched = len(candidates)` は top_k 合計であり探索数ではない | N を桁違いに過少計上 → DSR が楽観化（最も危険な方向） |
| L2 | **run 横断の系統が追えない** | `eml_alpha_runs` / `frost_runs` は run 単位で閉じており、同じ研究課題を何 run 繰り返したかの紐付けがない | 「10 run × 各 100 候補」が N=100 に見える |
| L3 | **候補の安定識別子がない** | `frost_runner.frost_candidates_from_eml` の `candidate_hash=str(hash(formula_text))[:16]` は PYTHONHASHSEED によりプロセスごとに変化する（実測で確認） | 同一式を run 間で同定できない。golden check の「candidate_hash は安定」という前提も実は成立していない |

## 2. 要件

1. **R1 完全計上**: 評価された試行は保存されない候補も含めて件数が残る（L1）
2. **R2 系統**: 同一研究課題（family）の試行を run 横断で合算でき、派生（親→子）関係で family を跨いで遡れる（L2）
3. **R3 as-of**: 昇格判定時点 t で「t 以前に記録された試行のみ」で N を数える。未来の試行で過去判定が変わらない
4. **R4 再現性**: DSR 判定は `(policy_hash, ledger_snapshot_hash)` で完全再現できる（D10 解消の延長）
5. **R5 改竄不能**: 台帳は append-only。UPDATE / DELETE は DB レベルで拒否する
6. **R6 軽量**: exhaustive は数万木に達しうるため、試行 1 件 = 1 行にはしない
7. **R7 既存非破壊**: 既存テーブルの変更なし（追加とビューのみ / リファクタリング計画の不変条件）。golden の決定を変えない

## 3. 選択肢

| 案 | 内容 | R1 | R2 | R3 | R5 | R6 | 判定 |
|---|------|----|----|----|----|----|------|
| A | 既存テーブルに `trial_count` 列を追加（knowledge_artifacts 等）してカウンタ加算 | △ | × | × | × | ○ | 却下: 可変カウンタは as-of 不能・改竄可能・派生を表現できない |
| **B** | **append-only の試行台帳（バッチ単位の件数 + SR 十分統計量）+ 系譜エッジ** | ○ | ○ | ○ | ○ | ○ | **採用** |
| C | 既存テーブル（eml_alpha_candidates / frost_fitness_candidates）からクエリ時に導出 | × | △ | ○ | ○ | ○ | 却下: 保存されない試行（L1）を原理的に数えられない |
| D | 試行 1 件 = 1 行の完全ログ | ○ | ○ | ○ | ○ | × | 却下: exhaustive の行数爆発。必要なのは件数と分散のみ |

## 4. 決定: B 設計

### 4.1 概念モデル

```
family (研究課題)                 ← family_key = H(horizon, universe, target, terminal_set_hash)
  └── trial batch (探索 1 回分)   ← 件数 n_trials + SR 十分統計量 (count, mean, M2)
        例: exhaustive depth≤3 で 4,212 木評価 → 1 行

lineage edge (派生)              ← parent family/candidate → child family/candidate
  relation: mutation | retrain | param_tweak | manual_edit | ensemble_member
```

- **試行 (trial)**: fitness を 1 回以上計算した候補式 1 つ。保存有無は問わない
- **family**: 「同じ問いに対する探索」の単位。family_key が同じ試行は同一の多重検定母集団とみなす
- **batch**: 1 回の探索呼び出し（exhaustive / gradient / 手動投入 等）。件数と SR 統計のみを持つ

### 4.2 N と V[SR] の定義

候補 c（family f）の時刻 t における DSR 入力:

```
F(c)      = f ∪ { 系譜エッジで f から遡れる全祖先 family }        # 派生元の探索コストも継承
N(c, t)   = Σ n_trials   over batches b ∈ F(c), b.recorded_at ≤ t
V[SR](c,t)= 並列分散合成 (Chan et al.) over 同 batch 群の (count, mean, M2)
             ※ sr_periodicity が DSR 入力と一致する batch のみ
```

- **N_raw（既定）**: 上記の単純合計。相関した試行を独立とみなすため DSR を**保守側**（厳しめ）に倒す
- **N_eff（将来オプション）**: alpha_genome_clusters のクラスタ数等で実効独立試行数に補正。P8 ablation 後に判断
- 子孫方向（c から派生した後続試行）は N に**含めない**（c 到達時点の探索コストのみが選択バイアス源）
- V[SR] の統計がない batch（exhaustive で rank IC のみ計算した等）は件数にのみ寄与。統計が 2 未満なら
  frost_dsr は SR 推定量分散へフォールバックする（既存挙動）

### 4.3 識別子

| ID | 生成 | 性質 |
|---|------|------|
| `formula_hash` | `sha256:` + SHA-256(空白正規化した式) | プロセス非依存・run 横断で安定（L3 の代替。既存 `candidate_hash` は変更しない） |
| `family_key` | `fam:` + SHA-256(canonical JSON{horizon, universe, target, terminal_set_hash})[:32] | 同条件なら常に同一 |
| `batch_id` | UUID5(namespace, `run_id|stage|seq`) | 再実行で同一 → ON CONFLICT DO NOTHING で冪等 |
| `ledger_snapshot_hash` | SHA-256(F(c) 内 batch_id のソート列 + as_of) | DSR 判定の再現キー（R4） |

### 4.4 スキーマ（migration 084）

```sql
qed_trial_batches (
  batch_id TEXT PK, family_key TEXT, run_id TEXT, trace_id TEXT,
  source_type TEXT, stage TEXT,                 -- exhaustive | gradient | manual | frost_eval ...
  n_trials INT CHECK (n_trials >= 0),
  sr_count INT, sr_mean DOUBLE PRECISION, sr_m2 DOUBLE PRECISION,   -- Welford 十分統計量
  sr_periodicity TEXT,                          -- daily | weekly | ... (非年率 SR の頻度)
  family_spec JSONB, metadata JSONB,
  recorded_at TIMESTAMPTZ DEFAULT now()
)
qed_lineage_edges (
  edge_id TEXT PK, parent_family_key, child_family_key,
  parent_formula_hash, child_formula_hash, relation, run_id, metadata, recorded_at
)
トリガ: 両テーブルの UPDATE / DELETE を RAISE EXCEPTION で拒否（R5）
ビュー: v_qed_family_trial_totals（family 単位の累計件数・batch 数・初回/最終記録時刻）
```

### 4.5 書き込み点（段階導入）

| 段 | 書き込み点 | 記録内容 | 本 ADR での扱い |
|---|---|---|---|
| S1 | `eml_search.exhaustive_search` / `gradient_search` | `len(trees)` / `n_init`（top_k 前の全評価数）+ fitness 統計 | **次フェーズ**（探索関数の戻り値に件数を追加する必要があり、golden 影響を検証してから） |
| S2 | `frost_runner.run_frost_pipeline` | FROST 評価候補数 + oos_sharpe 統計 | 次フェーズ |
| S3 | 手動投入 / 外部アルファ | `stage='manual'` で件数を申告 | 次フェーズ（CLI） |
| 読み出し | 昇格 Bridge → `DsrGate.check(n_trials=N, sr_variance=V)` | snapshot を audit に記録 | 次フェーズ |

本 ADR のスコープは **台帳の型・純関数・スキーマ・DB ブリッジ** まで。既存の探索・評価経路には一切触れない（R7）。

### 4.6 帰属の原則（過少計上の防止）

- 迷ったら**計上する**。N の過大は DSR を厳しくするだけだが、過少は偽の合格を生む（非対称リスク）
- family_key の粒度を変える（例: universe を粗くする）変更は PolicySpec 相当の重大変更として ADR 改訂を要する
- 台帳が空の family で DSR を求めた場合は、従来どおり `n_trials_source="assumed"` + `review_required=True`

## 5. 実装（本 ADR と同時に追加）

| ファイル | 内容 |
|---|---|
| `qedschema/migrations/084_qed_trial_ledger.sql` | §4.4 のテーブル・トリガ・ビュー |
| `analytics/python/frost/frost_lineage.py` | 純 Python: `formula_hash` / `make_family_key` / `SharpeStats` / `TrialBatch` / `LineageEdge` / `TrialLedger` / `TrialSnapshot` |
| `analytics/python/pg_io/postgres_lineage_bridge.py` | 台帳の insert（冪等）/ load / snapshot 取得 |
| `tests/unit/test_adr002_lineage.py` | DB レス単体テスト（marker `adr002_lineage`） |

## 6. 帰結

- **良い点**: N が「探索の実コスト」を反映する。DSR 判定が再現可能になる。台帳は改竄不能
- **コスト**: 探索関数に件数返却を追加する改修（S1）が必要。family_key の定義がガバナンス対象になる
- **既知の限界**: N_raw は相関試行を独立扱いするため過度に保守的になりうる → N_eff は P8 後に検討
- **対象外**: 人間の頭の中の試行（紙の上で捨てたアイデア）は数えられない。manual 申告で近似する

## 7. 設計憲法との整合

- 「バックテストは仮説検証の道具」→ 試した回数を隠さず台帳に残すこと自体が検証の前提
- 「Entry is technique, but exit is discipline」→ 過少計上を構造的に防ぐ（§4.6）ことを利便性より優先

## 8. 却下した代替案の補足

- **candidate_hash の修正で L3 を解決する案**: 正しい修正だが `UNIQUE(run_id, candidate_hash)` の UPSERT 挙動と
  golden dataset に影響するため、本 ADR には混ぜない（リファクタリング計画の外科原則）。TODO として §10 に退避し、
  系譜は独立した `formula_hash` を使う

## 9. 原ドラフトとの照合項目（原文が見つかった場合）

1. 「B 設計」が本 ADR の案 B（append-only 台帳）と同義か
2. family の定義粒度（本 ADR: horizon × universe × target × terminal_set）
3. 系譜エッジの relation 語彙
4. N_raw / N_eff の既定選択

## 10. TODO（本 ADR 外）

- [ ] `frost_runner.frost_candidates_from_eml` の `candidate_hash` を `formula_hash` 由来の安定値へ置換（golden 影響評価込み）
- [ ] S1: `exhaustive_search` / `gradient_search` が評価総数と fitness 統計を返すよう拡張
- [ ] 昇格 Bridge で `TrialLedger.snapshot()` → `DsrGate.check()` を配線し、snapshot_hash を audit_events へ
