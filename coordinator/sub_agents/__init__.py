"""Specialist sub-agents that live INSIDE the coordinator package.

Kept here (rather than in sibling folders) so that deploying `./coordinator`
ships everything the coordinator needs — no cross-package sibling imports that
would break in Agent Engine.
"""

from .market_agent import market_agent
from .financials_agent import financials_agent

__all__ = ["market_agent", "financials_agent"]
