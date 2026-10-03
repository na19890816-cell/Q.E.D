"""
test_p1_repairs.py
------------------
2026-10-03 P1 修理の回帰テスト (DB レス)

1. migration 079 / 080 / 081: 実在しない列参照・ドル引用の入れ子 (静的検査)
2. FROST CLI: 存在しない FrostConfig.from_env() 呼び出し
3. frost_runner: RunContext の非 UUID run_id → UUID 列への書き込み失敗
4. frost_runner: dry_run で policy_hash が記録されない
"""
from __future__ import annotations

import re
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIG = ROOT / "qedschema" / "migrations"

pytestmark = pytest.mark.p1_repairs


# ---------------------------------------------------------------------------
# 1. migration 静的検査
# ---------------------------------------------------------------------------

def _columns_of(table: str) -> set:
    """CREATE TABLE <table> ( ... ) の列名を素朴に抽出する。"""
    for f in sorted(MIG.glob("*.sql")):
        s = f.read_text(encoding="utf-8")
        m = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\((.*?)\n\s*\);", s, re.S)
        if m:
            cols = set()
            for line in m.group(1).splitlines():
                line = line.strip()
                mm = re.match(r"([a-z_][a-z0-9_]*)\s+[A-Z]", line)
                if mm and mm.group(1) not in ("constraint", "unique", "primary"):
                    cols.add(mm.group(1))
            return cols
    raise AssertionError(f"table {table} not found")


class TestMigrationStatic:
    def test_079_index_columns_exist(self):
        s = (MIG / "079_frost_indexes.sql").read_text(encoding="utf-8")
        for table, cols in re.findall(r"ON (\w+) \(([^)]*)\)", s):
            known = _columns_of(table)
            for c in cols.split(","):
                name = c.strip().split()[0]
                assert name in known, f"079: {table}.{name} は存在しない列"

    @pytest.mark.parametrize("bad", ["fc.batch_label", "fc.run_date", "fc.source_system",
                                     "fe.oos_sharpe_score", "fe.pbo_penalty", "fd.decided_at",
                                     "fd.frost_score_at_decision"])
    def test_080_no_phantom_columns(self, bad):
        s = (MIG / "080_frost_materialized_views.sql").read_text(encoding="utf-8")
        body = "\n".join(l for l in s.splitlines() if not l.strip().startswith("--"))
        assert bad not in body

    @pytest.mark.parametrize("f", sorted(p.name for p in MIG.glob("*.sql")))
    def test_no_nested_same_dollar_tag(self, f):
        """DO $$ ... $$ の内側で同じ $$ タグを使っていない (081 の再発防止)"""
        s = (MIG / f).read_text(encoding="utf-8")
        for block in re.findall(r"DO \$\$(.*?)END \$\$;", s, re.S):
            assert "$$" not in block, f"{f}: DO $$ ブロック内に $$ がある"


# ---------------------------------------------------------------------------
# 2-4. frost_runner
# ---------------------------------------------------------------------------

from analytics.python.frost.frost_runner import normalize_frost_run_id  # noqa: E402


class TestNormalizeRunId:
    def test_uuid_passthrough(self):
        u = str(uuid.uuid4())
        assert normalize_frost_run_id(u) == u

    def test_uuid_canonicalized(self):
        u = uuid.uuid4()
        assert normalize_frost_run_id(u.hex) == str(u)

    def test_runcontext_format_deterministic(self):
        a = normalize_frost_run_id("frost__20261003_024025")
        assert uuid.UUID(a) and a == normalize_frost_run_id("frost__20261003_024025")
        assert a != normalize_frost_run_id("frost__20261003_024026")

    @pytest.mark.parametrize("v", [None, ""])
    def test_empty_new_uuid(self, v):
        assert uuid.UUID(normalize_frost_run_id(v))


class TestFrostCli:
    def test_cli_starts(self, tmp_path):
        """旧実装は FrostConfig.from_env() で AttributeError → 必ず exit 1"""
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT), "QED_PG_DSN": "",
               "FROST_PG_DSN": ""}
        r = subprocess.run(
            [sys.executable, "-W", "ignore", "-m", "analytics.python.frost.frost_runner",
             "--dry-run", "--top-k", "7", "--batch-label", "cli_test"],
            capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120,
        )
        assert "AttributeError" not in r.stderr, r.stderr
        assert '"status"' in r.stdout, r.stdout + r.stderr

    def test_no_from_env_reference(self):
        src = (ROOT / "analytics/python/frost/frost_runner.py").read_text(encoding="utf-8")
        code = "\n".join(l.split("#")[0] for l in src.splitlines())
        assert "FrostConfig.from_env" not in code


class TestPolicyRecordedOnDryRun:
    def test_dry_run_still_upserts_policy(self):
        from analytics.python.frost import frost_runner as fr
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.policy_spec import policy_spec_from_frost_config
        cfg = FrostConfig(dry_run=True)
        spec = policy_spec_from_frost_config(cfg)
        out = MagicMock(run_id=str(uuid.uuid4()))
        with patch.object(fr, "upsert_policy_spec") as up, patch.object(fr, "set_run_policy_hash") as st:
            fr._upsert_policy(out, spec, cfg, conn=MagicMock())
        assert up.call_args.kwargs["dry_run"] is False
        assert st.call_args.kwargs["dry_run"] is False
