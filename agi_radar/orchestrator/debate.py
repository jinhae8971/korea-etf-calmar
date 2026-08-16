"""L3 — 3-Phase 토론 엔진과 Moderator.

설계 원칙: LLM 은 '선택적 상위 계층'이다.
ANTHROPIC_API_KEY 가 없거나 API 가 실패해도 파이프라인은 규칙 엔진 결과로
정상 종료한다. 토론은 규칙 판정을 뒤집는 게 아니라 근거를 서술한다.
"""

from __future__ import annotations

import json
import re

from agents.base_agent import STANCE_SCORE, AgentReport, format_evidence
from agents.personas import AGENT_CLASSES, CRITIQUE_PAIRS

RULE_TO_STANCE = {
    "THESIS_INTACT": "INTACT",
    "THESIS_STRESSED": "STRESSED",
    "THESIS_BROKEN": "BROKEN",
}


class DebateEngine:
    def __init__(self, agents: list):
        self.agents = agents

    def run(self, evidence: dict) -> dict:
        reports: list[AgentReport] = []
        for agent in self.agents:
            try:
                reports.append(agent.analyze(evidence))
            except Exception as exc:  # noqa: BLE001
                reports.append(
                    AgentReport(
                        agent_name=agent.name, role=agent.role, avatar=agent.avatar,
                        analysis=f"분석 실패 — {type(exc).__name__}", key_points=[],
                        confidence_score=0, stance="INTACT",
                    )
                )

        critiques = []
        for src, dst in CRITIQUE_PAIRS:
            if src >= len(reports) or dst >= len(reports):
                continue
            if reports[src].confidence_score == 0:
                continue
            try:
                critiques.append(self.agents[src].critique(reports[dst]).to_dict())
            except Exception:  # noqa: BLE001
                continue

        return {
            "phase1_reports": [r.to_dict() for r in reports],
            "phase2_critiques": critiques,
        }


class Moderator:
    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    @staticmethod
    def weighted_vote(reports: list[dict]) -> tuple[float, float]:
        total_w = weighted = conf = 0.0
        for report in reports:
            weight = report.get("confidence_score", 50)
            weighted += STANCE_SCORE.get(report.get("stance", "INTACT"), 0.0) * weight
            total_w += weight
            conf += weight
        if total_w == 0:
            return 0.0, 50.0
        return weighted / total_w, conf / len(reports)

    @staticmethod
    def score_to_state(score: float) -> str:
        if score > 0.35:
            return "THESIS_INTACT"
        if score < -0.35:
            return "THESIS_BROKEN"
        return "THESIS_STRESSED"

    def synthesize(self, debate: dict, evidence: dict, rule_verdict: dict) -> dict:
        reports = debate["phase1_reports"]
        vote_score, avg_conf = self.weighted_vote(reports)
        debate_state = self.score_to_state(vote_score)

        blocks = []
        for report in reports:
            blocks.append(
                f"[{report['agent_name']} / {report['stance']} / 확신 {report['confidence_score']}]\n"
                f"{report['analysis'][:500]}"
            )
        for critique in debate["phase2_critiques"]:
            blocks.append(f"[반론 {critique['from_agent']} → {critique['to_agent']}] {critique['critique'][:300]}")

        prompt = f"""아래는 AGI 투자 논지에 대한 4인 에이전트 토론입니다.

{format_evidence(evidence)}

=== 토론 ===
{chr(10).join(blocks)}

=== 선행 판단 ===
규칙 엔진: {rule_verdict['state']} (점수 {rule_verdict['score']})
토론 가중투표: {debate_state}

반드시 아래 JSON으로만 응답하세요.
{{
  "final_state": "THESIS_INTACT | THESIS_STRESSED | THESIS_BROKEN",
  "confidence_score": 65,
  "summary": "종합 판단 근거 200자 이상 한국어",
  "key_insights": ["인사이트1", "인사이트2", "인사이트3"],
  "risk_factors": ["리스크1", "리스크2"],
  "action_items": ["개인 투자자 관점의 구체적 행동 1", "행동 2"]
}}

주의: 단정하지 마세요. 관측된 정황과 그 해석을 구분해 서술하세요.
action_items 는 특정 종목 매수/매도 지시가 아니라 익스포저·사이징·점검 항목으로 쓰세요."""

        result: dict = {}
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=2000,
                system="당신은 토론 중재자입니다. 논리의 질과 데이터 신뢰성만으로 판단하고 JSON만 반환합니다.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = re.sub(r"```(?:json)?\s*|```\s*", "", resp.content[0].text).strip()
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                result = json.loads(match.group())
        except Exception:  # noqa: BLE001
            result = {}

        final_state = result.get("final_state")
        if final_state not in RULE_TO_STANCE:
            final_state = rule_verdict["state"]

        return {
            "final_state": final_state,
            "rule_state": rule_verdict["state"],
            "debate_state": debate_state,
            "confidence_score": int(result.get("confidence_score", avg_conf)),
            "summary": result.get("summary", ""),
            "key_insights": result.get("key_insights", []),
            "risk_factors": result.get("risk_factors", []),
            "action_items": result.get("action_items", []),
            "stance_votes": {r["agent_name"]: r["stance"] for r in reports},
            "llm": True,
        }


def run_debate(evidence: dict, rule_verdict: dict, api_key: str | None, model: str) -> dict:
    """LLM 토론 실행. 키가 없거나 실패하면 규칙 엔진 결과를 그대로 승격한다."""
    if not api_key:
        return _fallback(rule_verdict, "ANTHROPIC_API_KEY 미설정 — 규칙 엔진 판정만 사용"), {}
    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.Anthropic(api_key=api_key)
        agents = [cls(client, model) for cls in AGENT_CLASSES]
        debate = DebateEngine(agents).run(evidence)
        verdict = Moderator(client, model).synthesize(debate, evidence, rule_verdict)
        return verdict, debate
    except Exception as exc:  # noqa: BLE001
        return _fallback(rule_verdict, f"토론 계층 실패 ({type(exc).__name__}) — 규칙 엔진으로 폴백"), {}


def _fallback(rule_verdict: dict, note: str) -> dict:
    return {
        "final_state": rule_verdict["state"],
        "rule_state": rule_verdict["state"],
        "debate_state": None,
        "confidence_score": min(95, abs(int(rule_verdict["score"])) + 40),
        "summary": " / ".join(rule_verdict["reasons"]),
        "key_insights": rule_verdict["reasons"][:3],
        "risk_factors": [a["text"] for a in rule_verdict["alerts"]],
        "action_items": [],
        "stance_votes": {},
        "llm": False,
        "note": note,
    }
