"""
tests/unit/test_target_rule_resolver.py
-----------------------------------------
TargetRuleResolver のユニットテスト

- code 抽出ロジック (_extract_code) の各 strategy
- tag パターンのパース (candidate: / hypothesis:)
- ambiguous / unresolved / resolved の判定ロジック
- audit emit の呼び出し確認
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../analytics/python"))

from unittest.mock import MagicMock, patch
import pytest

from pg_io.postgres_event_study_target_rule_resolver import TargetRuleResolver


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _rule(
    *,
    rule_name: str = "candidate_code_direct",
    match_strategy: str = "candidate_code",
    source_field: str = "candidate_code",
    target_table: str = "factor_candidates",
    target_id_col: str = "id",
    target_code_col: str = "name",
) -> dict:
    return dict(
        rule_name=rule_name,
        match_strategy=match_strategy,
        source_field=source_field,
        target_table=target_table,
        target_id_col=target_id_col,
        target_code_col=target_code_col,
        priority=100,
        is_active=True,
    )


def _make_resolver(mock_conn=None, dry_run: bool = False):
    if mock_conn is None:
        mock_conn = MagicMock()
    mock_audit = MagicMock()
    with patch("pg_io.postgres_event_study_target_rule_resolver.TargetRuleCatalog") as MockCatalog:
        catalog_instance = MockCatalog.return_value
        catalog_instance.load_active_rules.return_value = []
        resolver = TargetRuleResolver(mock_conn, mock_audit, dry_run=dry_run)
        resolver.catalog = catalog_instance
    return resolver, mock_audit, catalog_instance


# ---------------------------------------------------------------------------
# _extract_code
# ---------------------------------------------------------------------------

class TestExtractCode:
    def setup_method(self):
        self.resolver, _, _ = _make_resolver()

    def test_candidate_code_strategy(self):
        rule = _rule(match_strategy="candidate_code", source_field="candidate_code")
        result = self.resolver._extract_code(rule, "", {"candidate_code": "12M モメンタム"})
        assert result == "12M モメンタム"

    def test_hypothesis_code_strategy(self):
        rule = _rule(match_strategy="hypothesis_code", source_field="hypothesis_code")
        result = self.resolver._extract_code(rule, "", {"hypothesis_code": "H-001"})
        assert result == "H-001"

    def test_tag_candidate_strategy_matched(self):
        rule = _rule(match_strategy="tag_candidate", source_field="")
        result = self.resolver._extract_code(rule, "candidate:some_factor", {})
        assert result == "some_factor"

    def test_tag_candidate_strategy_no_match(self):
        rule = _rule(match_strategy="tag_candidate", source_field="")
        result = self.resolver._extract_code(rule, "hypothesis:h1", {})
        assert result is None

    def test_tag_hypothesis_strategy_matched(self):
        rule = _rule(match_strategy="tag_hypothesis", source_field="")
        result = self.resolver._extract_code(rule, "hypothesis:H-002", {})
        assert result == "H-002"

    def test_unknown_strategy_returns_none(self):
        rule = _rule(match_strategy="unknown_strategy", source_field="")
        result = self.resolver._extract_code(rule, "any", {})
        assert result is None

    def test_missing_metadata_key_returns_none(self):
        rule = _rule(match_strategy="candidate_code", source_field="candidate_code")
        result = self.resolver._extract_code(rule, "", {})
        assert result is None


# ---------------------------------------------------------------------------
# _run_rules — resolved / ambiguous / unresolved
# ---------------------------------------------------------------------------

class TestRunRules:
    def _resolver_with_lookup(self, lookup_return: list) -> TargetRuleResolver:
        resolver, _, catalog = _make_resolver()
        catalog.load_active_rules.return_value = [_rule()]
        resolver._lookup = MagicMock(return_value=lookup_return)
        return resolver

    def test_resolved_when_one_match(self):
        import uuid
        target_id = uuid.uuid4()
        resolver = self._resolver_with_lookup([( target_id,)])
        result = resolver._run_rules(
            rules=[_rule()],
            artifact_id="art-1",
            artifact_tag="",
            metadata={"candidate_code": "12M モメンタム"},
        )
        assert result["resolution_status"] == "resolved"
        assert result["matched_target_id"] == str(target_id)
        assert result["matched_rule_name"] == "candidate_code_direct"

    def test_ambiguous_when_multiple_matches(self):
        import uuid
        resolver = self._resolver_with_lookup([(uuid.uuid4(),), (uuid.uuid4(),)])
        result = resolver._run_rules(
            rules=[_rule()],
            artifact_id="art-2",
            artifact_tag="",
            metadata={"candidate_code": "モメンタム"},
        )
        assert result["resolution_status"] == "ambiguous"
        assert result["candidate_count"] == 2

    def test_unresolved_when_no_match(self):
        resolver = self._resolver_with_lookup([])
        result = resolver._run_rules(
            rules=[_rule()],
            artifact_id="art-3",
            artifact_tag="",
            metadata={"candidate_code": "存在しないコード"},
        )
        assert result["resolution_status"] == "unresolved"

    def test_unresolved_when_no_code_extracted(self):
        resolver, _, _ = _make_resolver()
        result = resolver._run_rules(
            rules=[_rule(match_strategy="candidate_code", source_field="candidate_code")],
            artifact_id="art-4",
            artifact_tag="",
            metadata={},  # key なし
        )
        assert result["resolution_status"] == "unresolved"

    def test_priority_first_rule_wins(self):
        """優先度の高い (先の) ルールで resolved になればそこで終了。"""
        import uuid
        target_id = uuid.uuid4()
        rule1 = _rule(rule_name="rule_p1", match_strategy="candidate_code",
                      source_field="candidate_code")
        rule2 = _rule(rule_name="rule_p2", match_strategy="hypothesis_code",
                      source_field="hypothesis_code")
        resolver, _, catalog = _make_resolver()
        catalog.load_active_rules.return_value = [rule1, rule2]

        # rule1 → resolved, rule2 → never called
        lookup_calls = []
        def side_effect(*args, **kwargs):
            lookup_calls.append(args)
            return [(target_id,)]
        resolver._lookup = side_effect

        result = resolver._run_rules(
            rules=[rule1, rule2],
            artifact_id="art-5",
            artifact_tag="",
            metadata={"candidate_code": "code1", "hypothesis_code": "h1"},
        )
        assert result["resolution_status"] == "resolved"
        assert len(lookup_calls) == 1  # rule2 は呼ばれない


# ---------------------------------------------------------------------------
# resolve() — audit emit の確認
# ---------------------------------------------------------------------------

class TestResolveAuditEmit:
    def _setup(self, lookup_return):
        import uuid
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor

        # fetchone returns artifact row
        trace_id = str(uuid.uuid4())
        cursor.fetchone.return_value = (
            "art-001", trace_id, "event_study:run1", {"candidate_code": "12M モメンタム"}
        )

        mock_audit = MagicMock()
        with patch("pg_io.postgres_event_study_target_rule_resolver.TargetRuleCatalog") as MockCatalog:
            catalog = MockCatalog.return_value
            catalog.load_active_rules.return_value = [_rule()]
            resolver = TargetRuleResolver(conn, mock_audit, dry_run=True)
            resolver.catalog = catalog

        resolver._lookup = MagicMock(return_value=lookup_return)
        return resolver, mock_audit, trace_id

    def test_resolved_emits_applied(self):
        import uuid
        target_id = uuid.uuid4()
        resolver, mock_audit, _ = self._setup([(target_id,)])
        resolver.resolve("art-001")
        mock_audit.emit.assert_called_once()
        kwargs = mock_audit.emit.call_args[1]
        assert kwargs["decision"] == "APPLIED"
        assert kwargs["event_type"] == "TRANSITION_APPLIED"

    def test_unresolved_emits_rejected(self):
        resolver, mock_audit, _ = self._setup([])
        resolver.resolve("art-001")
        mock_audit.emit.assert_called_once()
        kwargs = mock_audit.emit.call_args[1]
        assert kwargs["decision"] == "REJECTED"
        assert kwargs["event_type"] == "TRANSITION_REJECTED"

    def test_trace_id_propagated_to_audit(self):
        import uuid
        target_id = uuid.uuid4()
        resolver, mock_audit, expected_trace = self._setup([(target_id,)])
        resolver.resolve("art-001")
        kwargs = mock_audit.emit.call_args[1]
        assert kwargs["trace_id"] == expected_trace
