"""Test bootstrap: make tools.* importable without Azure/pyodbc runtime deps.

Unit tests exercise pure logic only (validators, redaction, heuristics), so
heavy SDK modules are stubbed with MagicMocks when unavailable in the env.
"""
import sys
from unittest.mock import MagicMock

from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


def _ensure(name: str) -> None:
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = MagicMock()


for _mod in ("pyodbc", "azure", "azure.identity", "azure.search.documents",
             "azure.ai.projects"):
    _ensure(_mod)
