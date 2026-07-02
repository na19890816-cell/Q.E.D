"""
meta_validator.py
-----------------
FROST メタ検証レイヤー (Phase 8)。

バッチ評価 (FrostEvaluation / FrostDecision のリスト) を受け取り、
「決定の自己整合性」を横断検証するモジュール。

設計原則:
  - 副作用なし: 入力を変更しない
  - numpy 不使用 (ADR-001: frost_metrics / stability と同様の制約)
  - 標準ライブラリ + 型ヒントのみ
  - validate() は常に MetaValidationResult を返す (例外を外に漏らさない)

検証ルール:
  R01  SELECTED 決定は hard_gate_passed=True を要求  [ERROR]
  R02  decision_rank が付いた決定は frost_score で単調降順  [ERROR]
  R03  suppressed_by_dedup=True の決定が SELECTED になっていない  [ERROR]
  R04  pbo_score > pbo_threshold (デフォルト 0.5) かつ SELECTED  [WARNING]
  R05  frost_score < 0.0 を持つ FrostEvaluation  [ERROR]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# frost_contracts のみをインポート (外部依存なし)
from frost_contracts import FrostDecision, FrostEvaluation


# ---------------------------------------------------------------------------
# 公開定数
# ---------------------------------------------------------------------------

SEVERITY_ERROR   = "ERROR"
SEVERITY_WARNING = "WARNING"

RULE_R01 = "R01"
RULE_R02 = "R02"
RULE_R03 = "R03"
RULE_R04 = "R04"
RULE_R05 = "R05"

DEFAULT_PBO_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    """
    1 件のバリデーション問題。

    Attributes
    ----------
    severity : str
        "ERROR" または "WARNING"
    rule_name : str
        検証ルール識別子 ("R01" 〜 "R05")
    candidate_id : str
        問題のある候補の candidate_id。複数候補を横断するルール
        (R02 など) では空文字列を設定する場合がある。
    detail : str
        問題の詳細メッセージ (人間可読)
    """
    severity: str
    rule_name: str
    candidate_id: str
    detail: str

    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def is_warning(self) -> bool:
        return self.severity == SEVERITY_WARNING


@dataclass
class MetaValidationResult:
    """
    MetaValidator.validate() の戻り値。

    Attributes
    ----------
    issues : list of ValidationIssue
        全検証問題リスト (ERROR + WARNING の混在)
    error_count : int
        ERROR の件数
    warning_count : int
        WARNING の件数
    passed : bool
        ERROR が 0 件なら True (WARNING は passed を False にしない)
    """
    issues: List[ValidationIssue] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    passed: bool = True

    # ---- 便利メソッド -------------------------------------------------------

    def errors(self) -> List[ValidationIssue]:
        """ERROR 件のみ抽出。"""
        return [i for i in self.issues if i.is_error()]

    def warnings(self) -> List[ValidationIssue]:
        """WARNING 件のみ抽出。"""
        return [i for i in self.issues if i.is_warning()]

    def issues_for(self, candidate_id: str) -> List[ValidationIssue]:
        """指定 candidate_id に関連する問題を返す。"""
        return [i for i in self.issues if i.candidate_id == candidate_id]

    def issues_for_rule(self, rule_name: str) -> List[ValidationIssue]:
        """指定ルールに関連する問題を返す。"""
        return [i for i in self.issues if i.rule_name == rule_name]

    def summary(self) -> str:
        """単行サマリ文字列。"""
        status = "PASSED" if self.passed else "FAILED"
        return (
            f"MetaValidation {status}: "
            f"{self.error_count} error(s), {self.warning_count} warning(s)"
        )


# ---------------------------------------------------------------------------
# MetaValidator
# ---------------------------------------------------------------------------

class MetaValidator:
    """
    FROST バッチ評価結果の横断整合性検証クラス。

    使用方法
    --------
    validator = MetaValidator()
    result = validator.validate(evaluations, decisions)
    if not result.passed:
        for issue in result.errors():
            logging.error(issue.detail)

    Parameters
    ----------
    pbo_threshold : float
        R04 のしきい値 (デフォルト 0.5)。
        pbo_score > pbo_threshold かつ SELECTED の場合に WARNING を発行する。
    """

    def __init__(self, pbo_threshold: float = DEFAULT_PBO_THRESHOLD) -> None:
        self._pbo_threshold = float(pbo_threshold)

    # ------------------------------------------------------------------ #
    # 公開 API
    # ------------------------------------------------------------------ #

    def validate(
        self,
        evaluations: Sequence[FrostEvaluation],
        decisions: Sequence[FrostDecision],
    ) -> MetaValidationResult:
        """
        評価・決定リストを横断検証する。

        Parameters
        ----------
        evaluations : sequence of FrostEvaluation
        decisions   : sequence of FrostDecision

        Returns
        -------
        MetaValidationResult
            例外を外に漏らさず、常に結果オブジェクトを返す。
        """
        issues: List[ValidationIssue] = []

        try:
            # evaluation を candidate_id → FrostEvaluation の辞書に変換
            eval_map: Dict[str, FrostEvaluation] = {
                ev.candidate_id: ev for ev in evaluations if ev.candidate_id
            }

            issues.extend(self._check_r01(decisions, eval_map))
            issues.extend(self._check_r02(decisions))
            issues.extend(self._check_r03(decisions))
            issues.extend(self._check_r04(decisions, eval_map))
            issues.extend(self._check_r05(evaluations))

        except Exception as exc:  # pragma: no cover  — 防御的フォールバック
            issues.append(ValidationIssue(
                severity=SEVERITY_ERROR,
                rule_name="INTERNAL",
                candidate_id="",
                detail=f"MetaValidator 内部エラー: {exc}",
            ))

        error_count   = sum(1 for i in issues if i.is_error())
        warning_count = sum(1 for i in issues if i.is_warning())

        return MetaValidationResult(
            issues=issues,
            error_count=error_count,
            warning_count=warning_count,
            passed=(error_count == 0),
        )

    # ------------------------------------------------------------------ #
    # ルール別検証メソッド (プライベート)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_r01(
        decisions: Sequence[FrostDecision],
        eval_map: Dict[str, FrostEvaluation],
    ) -> List[ValidationIssue]:
        """
        R01: SELECTED 決定は hard_gate_passed=True を要求する。

        SELECTED なのに hard_gate_passed=False の場合は ERROR。
        evaluation が存在しない候補はスキップ（別レイヤーで保証する想定）。
        """
        issues = []
        for dec in decisions:
            if dec.decision != "SELECTED":
                continue
            ev = eval_map.get(dec.candidate_id)
            if ev is None:
                continue
            if not ev.hard_gate_passed:
                issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR,
                    rule_name=RULE_R01,
                    candidate_id=dec.candidate_id,
                    detail=(
                        f"[R01] candidate_id={dec.candidate_id!r} は SELECTED だが "
                        f"hard_gate_passed=False。"
                        f" gate_failures={ev.hard_gate_failures}"
                    ),
                ))
        return issues

    @staticmethod
    def _check_r02(decisions: Sequence[FrostDecision]) -> List[ValidationIssue]:
        """
        R02: decision_rank が付いた決定は frost_score で単調降順を確認する。

        decision_rank が None の決定はスキップ。
        ranked_decisions を decision_rank 昇順に並べ、
        frost_score が非増加 (monotone non-increasing) であることを検証。
        """
        issues = []
        ranked = [d for d in decisions if d.decision_rank is not None]
        if len(ranked) < 2:
            return issues

        # decision_rank 昇順 (rank=1 が最上位) でソート
        ranked_sorted = sorted(ranked, key=lambda d: d.decision_rank)  # type: ignore[arg-type]

        prev = ranked_sorted[0]
        for cur in ranked_sorted[1:]:
            if cur.frost_score > prev.frost_score + 1e-9:
                issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR,
                    rule_name=RULE_R02,
                    candidate_id=cur.candidate_id,
                    detail=(
                        f"[R02] decision_rank 単調性違反: "
                        f"rank={prev.decision_rank} (score={prev.frost_score:.6f}) → "
                        f"rank={cur.decision_rank} (score={cur.frost_score:.6f}) "
                        f"candidate_id={cur.candidate_id!r}"
                    ),
                ))
            prev = cur

        return issues

    @staticmethod
    def _check_r03(decisions: Sequence[FrostDecision]) -> List[ValidationIssue]:
        """
        R03: suppressed_by_dedup=True の決定が SELECTED になっていないことを確認。

        重複抑制されたはずの候補が誤って SELECTED になっている場合は ERROR。
        """
        issues = []
        for dec in decisions:
            if dec.suppressed_by_dedup and dec.decision == "SELECTED":
                issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR,
                    rule_name=RULE_R03,
                    candidate_id=dec.candidate_id,
                    detail=(
                        f"[R03] candidate_id={dec.candidate_id!r} は "
                        f"suppressed_by_dedup=True だが decision=SELECTED。"
                        f" near_duplicate_of={dec.near_duplicate_of!r}"
                    ),
                ))
        return issues

    def _check_r04(
        self,
        decisions: Sequence[FrostDecision],
        eval_map: Dict[str, FrostEvaluation],
    ) -> List[ValidationIssue]:
        """
        R04: pbo_score > pbo_threshold かつ SELECTED → WARNING。

        過学習確率が高い候補の採択は WARNING レベルで通知する。
        threshold は __init__ の pbo_threshold で設定可能。
        """
        issues = []
        for dec in decisions:
            if dec.decision != "SELECTED":
                continue
            ev = eval_map.get(dec.candidate_id)
            if ev is None:
                continue
            if ev.pbo_score > self._pbo_threshold:
                issues.append(ValidationIssue(
                    severity=SEVERITY_WARNING,
                    rule_name=RULE_R04,
                    candidate_id=dec.candidate_id,
                    detail=(
                        f"[R04] candidate_id={dec.candidate_id!r} は SELECTED だが "
                        f"pbo_score={ev.pbo_score:.4f} > threshold={self._pbo_threshold:.4f}。"
                        f" 過学習リスクに注意。"
                    ),
                ))
        return issues

    @staticmethod
    def _check_r05(evaluations: Sequence[FrostEvaluation]) -> List[ValidationIssue]:
        """
        R05: frost_score < 0.0 を持つ FrostEvaluation → ERROR。

        負のスコアはデータ異常または計算バグを示す。
        """
        issues = []
        for ev in evaluations:
            if ev.frost_score < 0.0:
                issues.append(ValidationIssue(
                    severity=SEVERITY_ERROR,
                    rule_name=RULE_R05,
                    candidate_id=ev.candidate_id,
                    detail=(
                        f"[R05] candidate_id={ev.candidate_id!r} の "
                        f"frost_score={ev.frost_score:.6f} が負値。不正な評価結果。"
                    ),
                ))
        return issues
