"""BaseAgent — 3-Phase 토론 프로토콜의 공통 인터페이스.

stance 값은 도메인에 맞게 재정의했다:
  ACCELERATE / INTACT / STRESSED / BROKEN
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

STANCES = ("ACCELERATE", "INTACT", "STRESSED", "BROKEN")
STANCE_SCORE = {"ACCELERATE": 1.0, "INTACT": 0.5, "STRESSED": -0.3, "BROKEN": -1.0}


@dataclass
class AgentReport:
    agent_name: str
    role: str
    avatar: str
    analysis: str
    key_points: list[str] = field(default_factory=list)
    confidence_score: int = 50
    stance: str = "INTACT"

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name, "role": self.role, "avatar": self.avatar,
            "analysis": self.analysis, "key_points": self.key_points,
            "confidence_score": self.confidence_score, "stance": self.stance,
        }


@dataclass
class AgentCritique:
    from_agent: str
    to_agent: str
    critique: str

    def to_dict(self) -> dict:
        return {"from_agent": self.from_agent, "to_agent": self.to_agent, "critique": self.critique}


class BaseAgent:
    name = ""
    role = ""
    avatar = "🤖"
    system_prompt = ""

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    # -- LLM ---------------------------------------------------------------
    def _call_llm(self, prompt: str, max_tokens: int = 1600) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    @staticmethod
    def parse_json(text: str) -> dict:
        text = re.sub(r"```(?:json)?\s*", "", text)
        text = re.sub(r"```\s*", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}

    # -- Phase 1 -----------------------------------------------------------
    def analyze(self, evidence: dict) -> AgentReport:
        prompt = f"""[증거 데이터]
{format_evidence(evidence)}

[분석 가이드]
{self.guide}

반드시 아래 JSON으로만 응답하세요. 다른 텍스트를 붙이지 마세요.
{{
  "analysis": "300자 이상의 한국어 분석",
  "key_points": ["핵심1", "핵심2", "핵심3"],
  "confidence_score": 70,
  "stance": "ACCELERATE | INTACT | STRESSED | BROKEN 중 하나"
}}"""
        raw = self._call_llm(prompt)
        data = self.parse_json(raw)
        stance = str(data.get("stance", "INTACT")).upper()
        if stance not in STANCES:
            stance = "INTACT"
        try:
            confidence = int(data.get("confidence_score", 50))
        except (TypeError, ValueError):
            confidence = 50
        return AgentReport(
            agent_name=self.name, role=self.role, avatar=self.avatar,
            analysis=str(data.get("analysis") or raw[:600]),
            key_points=[str(p) for p in (data.get("key_points") or [])][:5],
            confidence_score=max(0, min(100, confidence)),
            stance=stance,
        )

    # -- Phase 2 -----------------------------------------------------------
    def critique(self, other: AgentReport) -> AgentCritique:
        prompt = f"""당신의 분석 관점에서 아래 동료 분석에 반론을 제기하세요.

[{other.agent_name}의 분석]
판단: {other.stance} (확신도 {other.confidence_score})
주장: {other.analysis[:400]}

[반론 가이드]
- 상대 주장이 딛고 선 '가정'이나 '데이터 취약점'을 정확히 공격하세요.
- 150~250자, 한국어, 감정 표현 없이 논리적으로.
- 동의만 하지 말고 반드시 최소 하나의 반증 각도를 제시하세요."""
        return AgentCritique(self.name, other.agent_name, self._call_llm(prompt, 700).strip())

    guide = ""


def format_evidence(evidence: dict) -> str:
    """모든 에이전트에게 동일한 raw 증거를 제공한다 (해석은 각자)."""
    nodes = evidence.get("nodes", [])
    lines = ["■ 논지 노드별 상대강도 (벤치마크 대비 초과수익)"]
    for node in nodes:
        lines.append(
            f"  {node['rank']}위 {node['label']} [{node['role']}] "
            f"20일 {node['rs20']:+.2%} / 60일 {node['rs60']:+.2%} "
            f"(순위변동 {node['rank_delta']:+d}, 커버리지 {node['coverage']}/{node['universe']})"
        )
    hedge = evidence.get("hedge", {})
    lines += [
        "",
        "■ 헤지 유효성 (롱 바스켓 vs 숏 바스켓)",
        f"  상태 {hedge.get('status')} / 20일 상관 {hedge.get('corr20')} / "
        f"스프레드 변동성비 {hedge.get('spread_vol_ratio')}",
        f"  20일 롱 {hedge.get('long_return_20d')} vs 숏 {hedge.get('short_return_20d')} "
        f"→ 스프레드 {hedge.get('spread_return')}",
    ]
    crowd = evidence.get("crowding", {})
    lines += [
        "",
        f"■ 혼잡도 {crowd.get('score')} ({crowd.get('level')}) 구성 {crowd.get('components')}",
        f"  원지표 {crowd.get('raw')}",
    ]
    fund = evidence.get("funding", {})
    lines += ["", f"■ 자금조달 스트레스 {fund.get('level')} / {fund.get('values')}"]
    breadth_info = evidence.get("breadth", {})
    lines += ["", f"■ 논지 폭 {breadth_info.get('leading')}/{breadth_info.get('total')} 노드 초과수익"]
    lines += ["", f"■ 데이터 상태 {evidence.get('data_status')}"]
    return "\n".join(lines)
