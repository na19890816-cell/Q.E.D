# Dead Code Inventory
> Generated: 2026-07-02
> Tool: vulture 2.x  (min-confidence 60)
> Scope: `analytics/python/` `scripts/`
> **Note: 棚卸しのみ。このセッションでは削除しない（Phase 1+ で整理予定）**

## Summary

| Category | Count |
|----------|-------|
| unused variable | 61 |
| unused function | 56 |
| unused method | 11 |
| unused class | 4 |
| unused attribute | 1 |
| **Total** | **133** |

## Detail by File

### `analytics/python/alpha/eml/eml_ast_safety_proof.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 50 | unused function | `assert_no_future_leakage` | 60% |
| 84 | unused function | `save_safety_proof` | 60% |
| 131 | unused function | `load_safety_proof` | 60% |
| 165 | unused function | `summarize_proofs` | 60% |
| 196 | unused function | `build_ast_safety_record` | 60% |

### `analytics/python/alpha/eml/eml_core.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 95 | unused method | `from_json` | 60% |

### `analytics/python/alpha/eml/eml_lag_analyzer.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 73 | unused variable | `children` | 60% |
| 182 | unused function | `check_future_leakage` | 60% |
| 202 | unused function | `build_safety_proof` | 60% |
| 248 | unused function | `proof_to_dict` | 60% |

### `analytics/python/backtest/audit_hook.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 22 | unused function | `emit_backtest_audit` | 60% |

### `analytics/python/backtest/harness.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 65 | unused variable | `train_start` | 60% |
| 66 | unused variable | `train_end` | 60% |
| 91 | unused variable | `combined_net_returns` | 60% |
| 92 | unused variable | `combined_gross_returns` | 60% |
| 93 | unused variable | `combined_position` | 60% |
| 149 | unused variable | `train_dates` | 60% |

### `analytics/python/backtest/risk_gate.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 29 | unused variable | `max_trade_loss` | 60% |
| 30 | unused variable | `per_symbol_limit` | 60% |
| 31 | unused variable | `sector_limit` | 60% |
| 32 | unused variable | `total_exposure` | 60% |
| 39 | unused variable | `breadth_min` | 60% |

### `analytics/python/causal/causal_bridge.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 26 | unused function | `save_causal_run_result` | 60% |
| 112 | unused function | `load_causal_run_result` | 60% |

### `analytics/python/causal/causal_runner.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 198 | unused function | `run_causal_batch` | 60% |

### `analytics/python/features/event_window_features.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 14 | unused function | `build_event_window_mask` | 60% |
| 46 | unused function | `extract_event_window_features` | 60% |
| 56 | unused function | `compute_pre_event_drift` | 60% |

### `analytics/python/features/regime_features.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 85 | unused function | `build_regime_features` | 60% |

### `analytics/python/frost/frost_config.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 122 | unused variable | `use_v2_score` | 60% |
| 142 | unused variable | `min_causal_direction_score` | 60% |
| 145 | unused variable | `min_invariance_pass_ratio` | 60% |
| 148 | unused variable | `min_genome_novelty_score` | 60% |
| 151 | unused variable | `max_crowding_r2` | 60% |
| 154 | unused variable | `max_fsi` | 60% |
| 157 | unused variable | `min_regime_entropy` | 60% |
| 160 | unused variable | `max_signal_corr` | 60% |
| 208 | unused variable | `auto_approve_low_risk` | 60% |
| 223 | unused variable | `score_clip_min` | 60% |
| 226 | unused variable | `score_clip_max` | 60% |
| 231 | unused variable | `table_frost_runs` | 60% |
| 232 | unused variable | `table_frost_candidates` | 60% |
| 233 | unused variable | `table_frost_evaluations` | 60% |
| 234 | unused variable | `table_frost_decisions` | 60% |
| 235 | unused variable | `table_frost_promotion_bridges` | 60% |
| 236 | unused variable | `table_frost_audit_bridges` | 60% |
| 264 | unused method | `positive_weight_sum_v2` | 60% |
| 283 | unused method | `penalty_weight_sum_v2` | 60% |
| 508 | unused variable | `DEFAULT_CONFIG` | 60% |

### `analytics/python/frost/frost_contracts.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 64 | unused variable | `cost_model` | 60% |
| 75 | unused variable | `created_at` | 60% |
| 76 | unused variable | `updated_at` | 60% |
| 153 | unused variable | `created_at` | 60% |
| 154 | unused variable | `updated_at` | 60% |
| 202 | unused variable | `created_at` | 60% |
| 203 | unused variable | `updated_at` | 60% |
| 250 | unused method | `selected_candidates` | 60% |
| 254 | unused method | `promotion_eligible_decisions` | 60% |
| 258 | unused method | `rejected_decisions` | 60% |
| 262 | unused method | `get_evaluation` | 60% |
| 300 | unused variable | `created_at` | 60% |
| 335 | unused variable | `created_at` | 60% |
| 336 | unused variable | `updated_at` | 60% |

### `analytics/python/frost/frost_crowding.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 246 | unused function | `compute_crowding_score` | 60% |
| 407 | unused function | `crowding_to_frost_features` | 60% |
| 419 | unused function | `summarize_crowding_batch` | 60% |

### `analytics/python/frost/frost_decision_engine.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 33 | unused variable | `evaluations_by_cid` | 100% |

### `analytics/python/frost/frost_features.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 223 | unused variable | `max_aum_estimate` | 100% |

### `analytics/python/frost/frost_fragility_surface.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 179 | unused function | `compute_fragility_surface` | 60% |
| 441 | unused function | `fsi_to_score_components` | 60% |
| 463 | unused function | `fsi_hard_gate_pass` | 60% |
| 500 | unused function | `make_simple_eval_func` | 60% |
| 544 | unused function | `summarize_fsi_batch` | 60% |

### `analytics/python/frost/frost_known_factor_library.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 44 | unused variable | `factor_name` | 60% |
| 48 | unused variable | `typical_crowding_level` | 60% |
| 195 | unused variable | `FACTOR_LOOKUP` | 60% |
| 234 | unused function | `get_factor_families` | 60% |

### `analytics/python/frost/frost_metrics.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 53 | unused function | `robust_normalize` | 60% |
| 104 | unused function | `zscore_to_0_1` | 60% |
| 373 | unused function | `compute_scores_for_features` | 60% |
| 560 | unused function | `compute_scores_for_features_v2` | 60% |

### `analytics/python/frost/frost_pbo.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 40 | unused function | `_rank_values` | 60% |
| 119 | unused variable | `best_is_global_idx` | 60% |
| 144 | unused function | `estimate_pbo_is_oos_pairs` | 60% |

### `analytics/python/frost/frost_pbo_parallel.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 77 | unused variable | `n_workers_used` | 60% |
| 78 | unused variable | `parallel_enabled` | 60% |
| 106 | unused function | `run_pbo_parallel` | 60% |
| 152 | unused function | `build_pbo_tasks_from_evaluations` | 60% |

### `analytics/python/frost/frost_ranker.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 293 | unused variable | `selected_ids` | 60% |

### `analytics/python/frost/frost_report_builder.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 46 | unused function | `_decision_emoji` | 60% |

### `analytics/python/frost/frost_selector.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 69 | unused variable | `GATE_NAMES` | 60% |

### `analytics/python/frost/frost_signal_dedup.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 85 | unused variable | `candidate_ids` | 60% |
| 88 | unused variable | `corr_pairs` | 60% |
| 223 | unused function | `apply_signal_dedup` | 60% |
| 299 | unused attribute | `suppressed_by_signal_dedup` | 60% |

### `analytics/python/frost/frost_stability.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 178 | unused function | `compute_top_k_stability` | 60% |

### `analytics/python/frost/frost_surface_sampler.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 98 | unused variable | `perturbation_vector` | 60% |
| 99 | unused variable | `sample_index` | 60% |

### `analytics/python/frost/frost_worker_pool.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 60 | unused variable | `fastpath` | 60% |
| 62 | unused variable | `timeout_seconds` | 60% |
| 127 | unused function | `parallel_map_chunks` | 60% |

### `analytics/python/genome/alpha_genome_cluster.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 98 | unused function | `_vec_to_list` | 60% |

### `analytics/python/genome/alpha_genome_runner.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 149 | unused function | `run_genome_layer` | 60% |

### `analytics/python/genome/alpha_genome_similarity.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 83 | unused variable | `min_cosine_sim` | 60% |
| 130 | unused function | `build_similarity_lookup` | 60% |

### `analytics/python/io/postgres_eml_evaluation_writer.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 24 | unused function | `upsert_evaluations` | 60% |

### `analytics/python/io/postgres_frost_audit_bridge.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 250 | unused function | `emit_run_audit_events` | 60% |

### `analytics/python/io/postgres_frost_promotion_bridge.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 119 | unused function | `promote_frost_decisions` | 60% |
| 203 | unused function | `update_promotion_status` | 60% |
| 239 | unused function | `get_pending_promotions` | 60% |

### `analytics/python/metrics/portfolio.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 38 | unused function | `compute_portfolio` | 60% |

### `analytics/python/metrics/predictive.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 36 | unused function | `compute_predictive` | 60% |

### `analytics/python/metrics/regime.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 51 | unused function | `compute_regime` | 60% |

### `analytics/python/metrics/regime_entropy.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 471 | unused function | `compute_regime_entropy_from_features` | 60% |
| 497 | unused function | `regime_entropy_to_score_components` | 60% |
| 521 | unused function | `regime_entropy_hard_gate_pass` | 60% |
| 564 | unused function | `summarize_regime_entropy_batch` | 60% |

### `analytics/python/metrics/risk.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 36 | unused function | `compute_risk` | 60% |

### `analytics/python/metrics/trading.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 34 | unused function | `compute_trading` | 60% |

### `analytics/python/pg_io/postgres_artifact_link_target_catalog.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 38 | unused method | `upsert_rule` | 60% |

### `analytics/python/pg_io/postgres_event_study_artifact_links_bridge.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 21 | unused class | `ArtifactLinksBridge` | 60% |
| 34 | unused method | `create_links` | 60% |
| 61 | unused variable | `artifact_id_` | 60% |

### `analytics/python/pg_io/postgres_event_study_audit_bridge.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 17 | unused function | `fetch_audit_for_trace` | 60% |
| 72 | unused function | `fetch_resolution_summary` | 60% |

### `analytics/python/pg_io/postgres_event_study_experiment_report_bridge.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 27 | unused class | `ExperimentReportBridge` | 60% |
| 40 | unused method | `promote` | 60% |
| 59 | unused variable | `run_id_` | 60% |

### `analytics/python/pg_io/postgres_event_study_knowledge_artifact_bridge.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 26 | unused class | `KnowledgeArtifactBridge` | 60% |
| 39 | unused method | `promote` | 60% |
| 61 | unused variable | `run_id_` | 60% |

### `analytics/python/pg_io/postgres_event_study_target_rule_resolver.py`

| Line | Kind | Symbol | Conf |
|------|------|--------|------|
| 40 | unused class | `TargetRuleResolver` | 60% |
| 76 | unused variable | `artifact_id_` | 60% |

