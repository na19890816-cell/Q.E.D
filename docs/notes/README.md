# docs/notes — Note 登録インデックス

検証サイクルに載せるべき未実装項目・検討課題を番号付きで管理する。  
Note は「今すぐコードを書かない」が「忘れてはいけない」項目の台帳。  
実装に着手する際は対応する Note に `ステータス: 実装中` を記録し、完了後に `解消済み` とする。

---

## 憲法ギャップ（設計憲法が義務付けているが未実装）

| Note | タイトル | 優先度 | 配置 | ステータス |
|---|---|---|---|---|
| [NOTE-001](NOTE-001-deflated-sharpe-ratio.md) | Deflated Sharpe Ratio（DSR）未実装 | **最優先** | P8 + ADR-002 先行 | Note登録済み |
| [NOTE-002](NOTE-002-portfolio-correlation-gate.md) | 採用済みポートフォリオとの相関ゲート r<0.6 未実装 | 高 | P8 ablation 後 | ✅ 実装完了 (2026-09-30) |
| [NOTE-003](NOTE-003-detect-kill-lifecycle.md) | Detect → Kill ライフサイクル未実装 | 高 | P0〜P9 完了後の第1機能追加 | ✅ 実装完了 (2026-09-30) |

## 追加ゲート候補（gate-0 評価待ち）

| Note | タイトル | 優先度 | 配置 | ステータス |
|---|---|---|---|---|
| [NOTE-004](NOTE-004-min-signal-count-gate.md) | 最小シグナル数ゲート | 中 | P8 ablation 後に gate-0 評価 | Note登録済み |
| [NOTE-005](NOTE-005-train-val-gap-gate.md) | train/val gap 直接ゲート | 中 | P8 ablation 後に gate-0 評価 | Note登録済み |

---

## 実装ブロッカー関係図

```
ADR-002 系譜ログ (B 設計)
    └── NOTE-001 (DSR): 試行数 N 集計基盤として必要

P8 軸 ablation（既存軸の寄与計測）
    ├── NOTE-001 (DSR)     : P8 内で実装
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

**最終更新**: 2026-06-13（QED_REVIEW_2026-06-13.md §6 指示に基づき登録）
