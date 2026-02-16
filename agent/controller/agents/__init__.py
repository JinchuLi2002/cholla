"""Deterministic mock advisory agents for the hybrid controller."""

from __future__ import annotations

from agent.controller.agents.planner import MockPlannerAgent
from agent.controller.agents.summarizer import MockSummarizerAgent

__all__ = ["MockPlannerAgent", "MockSummarizerAgent"]
