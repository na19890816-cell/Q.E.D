"""
postgres_artifact_link_target_catalog.py
-----------------------------------------
target_resolution_rules カタログの管理ユーティリティ。
seed / 取得 / 更新を担当する。
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg import Connection

logger = logging.getLogger(__name__)


class TargetRuleCatalog:
    """target_resolution_rules の CRUD。"""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def load_active_rules(self) -> list[dict[str, Any]]:
        """優先順位昇順でアクティブなルールを返す。"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT rule_name, priority, match_strategy, source_field,
                       target_table, target_id_col, target_code_col
                FROM target_resolution_rules
                WHERE is_active = true
                ORDER BY priority ASC
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def upsert_rule(
        self,
        rule_name: str,
        priority: int,
        match_strategy: str,
        source_field: str,
        target_table: str,
        target_code_col: str,
        target_id_col: str = "id",
        description: str = "",
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO target_resolution_rules
                    (rule_name, priority, match_strategy, source_field,
                     target_table, target_id_col, target_code_col, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_name) DO UPDATE SET
                    priority        = EXCLUDED.priority,
                    match_strategy  = EXCLUDED.match_strategy,
                    source_field    = EXCLUDED.source_field,
                    target_table    = EXCLUDED.target_table,
                    target_id_col   = EXCLUDED.target_id_col,
                    target_code_col = EXCLUDED.target_code_col,
                    description     = EXCLUDED.description,
                    is_active       = true
                """,
                (rule_name, priority, match_strategy, source_field,
                 target_table, target_id_col, target_code_col, description),
            )
