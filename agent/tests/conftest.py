"""Test bootstrap: make tools.* importable without Azure/pyodbc runtime deps.

Unit tests exercise pure logic only (validators, redaction, heuristics), so
heavy SDK modules are stubbed when unavailable in the env.

Stubs are registered as *package-like* modules rather than bare MagicMocks.
A bare mock in sys.modules breaks `from azure.search.documents.models import X`:
the import machinery finds `azure` but a MagicMock is not a package, so the
dotted import dies before the test collects. Giving each stub an empty
`__path__` plus a module-level `__getattr__` makes every depth resolve.
"""
import sys
import types
from unittest.mock import MagicMock

from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))


def _stub(name: str) -> types.ModuleType:
    """Register `name` and any missing parents as inert package modules."""
    parts = name.split(".")
    module: types.ModuleType | None = None
    for depth in range(1, len(parts) + 1):
        dotted = ".".join(parts[:depth])
        module = sys.modules.get(dotted)
        if module is None:
            module = types.ModuleType(dotted)
            module.__path__ = []  # package-like: dotted imports keep resolving

            def _getattr(attr: str, _self=module) -> MagicMock:
                if attr.startswith("__"):
                    raise AttributeError(attr)
                value = MagicMock(name=f"{_self.__name__}.{attr}")
                setattr(_self, attr, value)
                return value

            module.__getattr__ = _getattr  # PEP 562
            sys.modules[dotted] = module
    return module  # type: ignore[return-value]


def _ensure(name: str) -> None:
    """Leave a genuinely installed module alone; stub it only if it is missing."""
    try:
        __import__(name)
    except ImportError:
        _stub(name)


for _mod in (
    "pyodbc",
    "azure",
    "azure.identity",
    "azure.search.documents",
    "azure.search.documents.models",
    "azure.ai.projects",
):
    _ensure(_mod)
