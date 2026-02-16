"""Advisory agents for the hybrid controller."""

from __future__ import annotations

from agent.controller.agents.planner_lm import PlannerLMAgent
from agent.controller.agents.planner import MockPlannerAgent
from agent.controller.agents.summarizer_lm import SummarizerLMAgent
from agent.controller.agents.summarizer import MockSummarizerAgent

PlannerAgent = PlannerLMAgent
SummarizerAgent = SummarizerLMAgent

__all__ = [
    "MockPlannerAgent",
    "MockSummarizerAgent",
    "PlannerAgent",
    "PlannerLMAgent",
    "SummarizerAgent",
    "SummarizerLMAgent",
]
