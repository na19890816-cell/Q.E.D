# docs/notes — Note 登録インデックス

検証サイクルに載せるべき未実装項目・検討課題を番号付きで管理する。  
Note は「今すぐコードを書かない」が「忘れてはいけない」項目の台帳。  
実装に着手する際は対応する Note に `ステータス: 実装中` を記録し、完了後に `解消済み` とする。

---

## 憲法ギャップ（設計憲法が義務付けているが未実装）

| Note | タイトル | 優先度 | 配置 | ステータス |
|---|---|---|---|---|
| [NOTE-001](NOTE-001-deflated-sharpe-ratio.md) | Deflated Sharpe Ratio（DSR）未実装 | **最優先** | P8 + ADR-002 先行 | ✅ 実装完了 (2026-09-30) ※N は ADR-002 まで暫定 |
| [NOTE-002](NOTE-002-portfolio-correlation-gate.md) | 採用済みポートフォリオとの相関ゲート r<0.6 未実装 | 高 | P8 ablation 後 | ✅ 実装完了 (2026-09-30) |
| [NOTE-003](NOTE-003-detect-kill-lifecycle.md) | Detect → Kill ライフサイクル未実装 | 高 | P0〜P9 完了後の第1機能追加 | ✅ 実装完了 (2026-09-30) |

## 追加ゲート候補（gate-0 評価待ち）

| Note | タイトル | 優先度 | 配置 | ステータス |
|---|---|---|---|---|
| [NOTE-004](NOTE-004-min-signal-count-gate.md) | 最小シグナル数ゲート | 中 | P8 ablation 後に gate-0 評価 | Note登録済み |
| [NOTE-005](NOTE-005-train-val-gap-gate.md) | train/val gap 直接ゲート | 中 | P8 ablation 後に gate-0 評価 | Note登録済み |

## 運用・探索設計（2026-10-03 追加）

| Note | タイトル | 優先度 | 配置 | ステータス |
|---|---|---|---|---|
| [NOTE-006](NOTE-006-exhaustive-search-degeneracy.md) | exhaustive 探索の縮退（93,347 木 → 17 式） | 中 | 案 A は即時可 / 案 B・C は gate-0 後 | Note登録済み |
| [NOTE-007](NOTE-007-promotion-bridge-format-bug-impact.md) | 既存バグ（昇格 Bridge / migration / CLI / hash）の本番影響確認 | **高** | 本番 DB で確認 SQL を実行 | 確認待ち |
| [NOTE-008](NOTE-008-p8-meta-validation-structural-findings.md) | P8 メタ検証の構造的所見（regime 軸の定数化 / top_k 非拘束 / 昇格境界の僅差） | 中 | golden 抽出後に再測定 → gate-0 | Note登録済み |

---

## 実装ブロッカー関係図

```
ADR-002 系譜ログ (B 設計)  ← 2026-09-30 Proposed: docs/adr/ADR-002-lineage-trial-ledger.md（台帳本体は実装済・書き込み点は未配線）
    └── NOTE-001 (DSR): 試行数 N 集計基盤として必要（DSR 本体は実装済・N 暫定）

P8 軸 ablation（既存軸の寄与計測）
    ├── NOTE-001 (DSR)     : ✅ 実装完了（gate_engine 統合は P8 後に判断）
    ├── NOTE-002 (相関ゲート): ✅ 実装完了
    ├── NOTE-004 (最小シグナル数): gate-0 評価後
    └── NOTE-005 (train/val gap): gate-0 評価後

P0〜P9 完了
    └── NOTE-003 (Detect→Kill): リファクタ完了後の第1機能追加
```

---

## 追加ルール

- Note は「コード変更なし」の状態で登録する
- 実装着手は必ず `gate-0`（増分価値の測定）を通してから
- 憲法ギャップ（NOTE-001〜003）は gate-0 不要で採用決定済み、ただし実装順序に制約あり
- Note のステータス変更は対応する Note ファイルと本インデックスを同時に更新する

---

**最終更新**: 2026-10-03（NOTE-006 / NOTE-007 登録）
