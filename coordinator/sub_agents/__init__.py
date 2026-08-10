"""Specialist sub-agents that live INSIDE the coordinator package.

Kept here rather than in sibling folders so that deploying `./coordinator` ships
everything the coordinator needs — no cross-package imports that would break
once the folder is deployed on its own.

Each specialist is exposed as a BUILDER, not a module-level instance, so the
coordinator can assemble a graph containing only the lanes that are actually
available and can build more than one graph in a process without sharing agent
objects between them.
"""

from .document_agent import build_document_agent
from .financials_agent import build_financials_agent
from .market_agent import build_market_agent

__all__ = [
    "build_document_agent",
    "build_financials_agent",
    "build_market_agent",
]
