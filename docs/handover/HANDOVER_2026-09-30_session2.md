# 作業引き継ぎ書 — 2026-09-30 (セッション 2)

**プロジェクト**: ProStock / Q.E.D. — FROST Meta-Fitness Engine  
**リポジトリ**: `https://github.com/na19890816-cell/Q.E.D.git`  
**ブランチ**: `main`  
**前回引き継ぎ書**: `docs/handover/HANDOVER_2026-09-30.md` (Phase 3 完了時点, commit `85cde61`)  
**テスト状態**: ✅ `1315 passed, 24 skipped` (skip は全て `QED_PG_DSN` 未設定の統合テスト)

---

## 1. 本セッションの完了作業

### Phase 4a — G3 CUSUM パラメータの PolicySpec 集約 (commit `891ea93`)

前回引き継ぎ書 §6「PolicySpec への G3 パラメータ追加（未着手）」を解消。

| 追加フィールド | 環境変数 | デフォルト |
|---|---|---|
| `cusum_k` | `FROST_CUSUM_K` | 0.5 |
| `cusum_h` | `FROST_CUSUM_H` | 5.0 |
| `cusum_mu0` | `FROST_CUSUM_MU0` | 0.0 |
| `lifecycle_min_ic_len` | `FROST_LIFECYCLE_MIN_IC_LEN` | 10 |

- `policy_spec.py`: フィールド / `to_dict` / `from_dict` / `validate` / 両ブリッジ関数 / `load_policy_spec` / `_POLICY_ENV_VARS`
- `frost_config.py`: フィールド / `load_frost_config` / `validate`
- **ついでの修正**: G2 実装時に `_POLICY_ENV_VARS` へ `FROST_MAX_PORTFOLIO_CORR` が登録漏れしていたのを追加
- 既存の `CusumParams.from_config` / `AlphaLifecycleEngine.from_config` は getattr ベースのため無変更で連動
- `tests/unit/test_phase4_policy_g3_params.py` (38 tests, marker `phase4_policy_g3`)

### Phase 4b — G1 Deflated Sharpe Ratio (commit `e42b14b`)

NOTE-001（憲法ギャップ最優先）を実装。

- `analytics/python/frost/frost_dsr.py` 新規（**純 Python**。決定経路のため ADR-001 numpy ホワイトリスト外と判断）
  - `norm_cdf` (math.erfc) / `norm_ppf` (Acklam + Halley 補正, scipy 比誤差 < 1e-8)
  - `sample_moments` → `ReturnMoments(n_obs, mean, std(ddof=1), skew, kurt[非超過])`
  - `probabilistic_sharpe_ratio` / `expected_max_sharpe` / `deflated_sharpe_ratio` / `cross_trial_sr_variance`
  - `DsrParams(min_dsr=0.95, default_n_trials=1)` frozen
  - `DsrGate.check(returns, n_trials, trial_sharpes, sr_variance)` / `.check_stats(...)` / `.from_config`
  - `DsrGateResult.to_dict()` (JSON 化可, inf→None)
  - `check_dsr_gate(...)` 関数型ラッパー
- PolicySpec / FrostConfig に `min_dsr=0.95` / `dsr_default_n_trials=1` 追加（env `FROST_MIN_DSR`, `FROST_DSR_DEFAULT_N_TRIALS`）
- `tests/unit/test_phase4_dsr.py` (116 tests, marker `phase4_dsr`)
- **論文数値例を再現**: 年率 SR 2.5 / T=1250 / N=100 / V=0.5 / 歪度 −3 / 尖度 10 → DSR = 0.9004
- `test_phase1_policy_spec.py::test_to_dict_hard_gates_count` 16 → 22

### ドキュメント
- `docs/notes/NOTE-001-deflated-sharpe-ratio.md` §8 実装記録追加・ステータス更新
- `docs/notes/README.md` インデックス更新
- `docs/runbooks/frost_promotion_policy.md` §16 DSR ゲート追記

---

## 2. 重要な設計判断（要確認事項）

1. **数式の訂正**: NOTE-001 §2 / 前回引き継ぎ書 §5 の DSR 式は原論文と一致しなかったため、原論文
   `DSR = PSR(SR0)` に従った。DSR は確率値 [0,1] のため、不合格条件は「DSR < 0」ではなく **「DSR < min_dsr (0.95)」**。
2. **N の暫定扱い**: ADR-002 未整備のため N 未提供時は `dsr_default_n_trials`（=1 → DSR = PSR(0) の退化版）を使用し、
   `n_trials_source="assumed"` + **`review_required=True` を強制**（N 過少申告は DSR を楽観化するため）。
3. **配置**: G2 と同じく昇格 Bridge から呼ぶスタンドアロンゲート。`gate_engine.py` / 10 軸スコアへの統合は **P8 ablation 後に判断**
   （golden harness の決定を変えないため、既存評価経路は一切変更していない）。
4. **PolicySpec のハッシュ変化**: hard_gates に 6 フィールド追加したため、**デフォルト PolicySpec の `policy_hash` が変わった**。
   既存 `qed_policies` 行とは別ポリシーとして登録される（`from_dict` は旧 dict も欠損キーをデフォルトで復元可能）。

---

## 3. 憲法ギャップ解消状況

| Gap | Note | タイトル | ステータス |
|-----|------|---------|---------|
| G1 | NOTE-001 | Deflated Sharpe Ratio | ✅ 実装完了 (`e42b14b`) ※N は ADR-002 まで暫定 |
| G2 | NOTE-002 | ポートフォリオ相関ゲート r<0.60 | ✅ 実装完了 (`8f2997f`) |
| G3 | NOTE-003 | Detect→Kill ライフサイクル | ✅ 実装完了 (`85cde61`) + PolicySpec 集約 (`891ea93`) |
| G4 | NOTE-004 | 最小シグナル数ゲート | ⏳ gate-0 評価待ち |
| G5 | NOTE-005 | train/val gap 直接ゲート | ⏳ gate-0 評価待ち |

---

## 4. 次セッションへの引き継ぎ（推奨順）

1. **ADR-002 系譜ログ設計**（DSR の N 供給元。現状唯一の G1 残課題）
   - `knowledge_artifacts` に `lineage_id` / `trial_count` を持たせるか、別テーブルで run 横断試行を集計
   - 集計後、昇格 Bridge で `DsrGate.check(returns, n_trials=..., trial_sharpes=...)` に供給
2. **昇格 Bridge への G1/G2 ゲート配線**: 現在 `DsrGate` / `PortfolioCorrelationGate` はどちらも
   本番コードから呼ばれていない（テストのみ）。`postgres_event_study_knowledge_artifact_bridge.py` 等の
   昇格フローに組み込み、結果を audit_events に記録する
3. **P8 軸 ablation** → DSR の gate_engine / スコア軸統合可否、NOTE-004 / 005 の gate-0 評価
4. `qedschema/migrations/` に DSR 結果列（または frost_promotion_bridges の evidence JSON）を追加するか検討

---

## 5. コマンド

```bash
cd /home/user/prostock
python3 -W ignore -m pytest tests/ -q --tb=no           # 1315 passed, 24 skipped
python3 -W ignore -m pytest -m phase4_dsr -q             # 116
python3 -W ignore -m pytest -m phase4_policy_g3 -q       # 38

# DSR smoke
python3 -c "
from analytics.python.frost.frost_dsr import deflated_sharpe_ratio
import math
print(deflated_sharpe_ratio(2.5/math.sqrt(250), 1250, 100, -3, 10, sr_variance=0.5/250))  # 0.9004
"
```

## 6. 守るべき設計原則（変更なし）

ADR-001（statistics 禁止・numpy はホワイトリストのみ） / DB レス単体テスト / 純関数 + dataclass /
半自動 Kill / PolicySpec 中心（全パラメータ集約・policy_hash 更新） / frozen=True / 1 フェーズ = 1 コミット
