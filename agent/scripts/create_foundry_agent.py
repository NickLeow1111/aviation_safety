#!/bin/sh
'''exec' "$(dirname "$0")/../.venv/bin/python" "$0" "$@" # '''

"""Register the Safety Intelligence Bot as a Foundry v2 PromptAgent.

Uses the new (non-deprecated) Microsoft Foundry Agents v2 API:
    azure-ai-projects 2.x  ->  AIProjectClient.agents.create_version(...)
    with PromptAgentDefinition + AzureAISearchTool + FunctionTool.

After this runs, set FOUNDRY_AGENT_NAME=<printed name> on the Container App
(NOT the legacy asst_xxx ID). The Container App must call the v2 Responses
runtime — see the note at the bottom of this file for what that means for
agent.py.
"""

import os
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchTool,
    AzureAISearchToolResource,
    FunctionTool,
    PromptAgentDefinition,
)
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_ROOT = SCRIPT_DIR.parent
# Allow imports from agent/tools regardless of CWD.
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

load_dotenv(AGENT_ROOT / ".env.foundry")
load_dotenv(AGENT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ENDPOINT = os.environ.get("AZURE_AI_PROJECT_ENDPOINT", "").strip()
if not ENDPOINT.startswith("https://"):
    raise SystemExit(
        f"AZURE_AI_PROJECT_ENDPOINT is missing or not https (got {ENDPOINT!r}). "
        "Set it in agent/.env or agent/.env.foundry, or export the Bicep output "
        "'foundryProjectEndpoint' before running."
    )

MODEL_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "safety-intelligence-bot")
SEARCH_INDEX = os.environ.get("SEARCH_INDEX", "safety-docs")

PROMPTS = AGENT_ROOT / "prompts"


def _build_instructions() -> str:
    """system.md + live schema reference + dynamic scope + few-shot examples.

    When Synapse is reachable at agent-creation time, the static
    'Deployment data scope' section is REPLACED with what is actually
    deployed (view list + column reference), so the prompt can never drift
    from the database. Falls back to the static text when unreachable.
    """
    from tools import schema_catalog

    system_text = (PROMPTS / "system.md").read_text()

    marker = "\n## Deployment data scope"
    base, sep, _static_scope = system_text.partition(marker)

    live_scope = schema_catalog.format_scope_section()
    live_ref = schema_catalog.format_schema_reference()
    if sep and live_scope and live_ref:
        view_count = len(schema_catalog.get_schemas() or {})
        system_text = base + "\n" + live_scope + "\n" + live_ref
        print(f"Injected LIVE deployment scope ({view_count} views) into instructions.")
    else:
        print(
            "WARNING: live schema unavailable — keeping STATIC deployment "
            "scope from system.md. Re-run after Synapse is reachable."
        )

    return (
        system_text
        + "\n\n---\n\n"
        + (PROMPTS / "nl2sql_examples.md").read_text()
    )


INSTRUCTIONS = _build_instructions()

# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
project = AIProjectClient(endpoint=ENDPOINT, credential=DefaultAzureCredential())

# Find the AI Search project connection by *type* (resilient to renames).
search_conn = next(
    (c for c in project.connections.list()
     if str(getattr(c, "type", "")).lower().endswith("azure_ai_search")),
    None,
)
if search_conn is None:
    raise SystemExit(
        "No Azure AI Search connection found on this project. "
        "Add it in the Foundry portal (Management center → Connections) first."
    )
print(f"Using search connection: {search_conn.name}  ({search_conn.id})")

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
ai_search_tool = AzureAISearchTool(
    azure_ai_search=AzureAISearchToolResource(
        indexes=[
            AISearchIndexResource(
                project_connection_id=search_conn.id,
                index_name=SEARCH_INDEX,
                query_type=AzureAISearchQueryType.SEMANTIC,
                top_k=8,
            )
        ]
    )
)

nl2sql_tool = FunctionTool(
    name="nl2sql",
    description=(
        "Run a single read-only T-SQL SELECT against the vw_SafetyIntel_* views "
        "in Synapse. Use for any structured data question (counts, lists, trends). "
        "Do NOT use for free-text/document questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": (
                    "A single T-SQL SELECT statement. Must reference only "
                    "vw_SafetyIntel_* views. No DML/DDL, no chained statements."
                ),
            }
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
    strict=True,
)

chart_tool = FunctionTool(
    name="chart_spec",
    description=(
        "Convert tabular rows from a prior nl2sql call into a Vega-Lite chart spec. "
        "MUST be called AFTER nl2sql and MUST pass the actual rows returned by "
        "nl2sql in the `rows` argument — never an empty array."
    ),
    parameters={
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["bar", "line", "pie", "area", "scatter", "heatmap", "table"],
                "description": "Visualization type. Use 'line'/'area' for trends over time, 'bar' for categorical comparisons, 'pie' for share of whole, 'heatmap' for 2-D distributions, 'table' for raw lists.",
            },
            "rows": {
                "type": "array",
                "items": {"type": "object"},
                "minItems": 1,
                "description": "Array of row objects returned by nl2sql. MUST contain at least one row; never pass an empty array.",
            },
            "x": {"type": "string", "description": "Column name for the x-axis (optional; auto-picked if omitted)."},
            "y": {"type": "string", "description": "Column name for the y-axis / measure (optional; auto-picked if omitted)."},
            "color": {"type": "string", "description": "Column name for color encoding / series split (optional)."},
            "title": {"type": "string", "description": "Chart title (optional)."},
        },
        "required": ["intent", "rows"],
        "additionalProperties": False,
    },
    strict=False,
)

dashboard_tool = FunctionTool(
    name="dashboard_spec",
    description=(
        "Assemble multiple related result sets from prior nl2sql calls into a single "
        "operations-dashboard artifact for occurrence monitoring screens. Use for runway "
        "incursion, bird strike, or similar cockpit-style dashboards."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Dashboard title."},
            "domain": {"type": "string", "description": "Domain name such as runway_incursion or bird_strike."},
            "focus": {"type": "string", "description": "Short user-facing focus statement."},
            "datasets": {
                "type": "array",
                "minItems": 1,
                "description": "Named datasets assembled from prior nl2sql responses.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Dataset role such as overview, tracks, hotspots, alerts, tactical_audit, or recent_records."},
                        "title": {"type": "string", "description": "Optional dataset title."},
                        "rows": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "The actual rows returned by nl2sql for this dataset."
                        }
                    },
                    "required": ["name", "rows"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["datasets"],
        "additionalProperties": False,
    },
    strict=False,
)

schema_tool = FunctionTool(
    name="get_schema",
    description=(
        "Look up the live schema of vw_SafetyIntel_* views in Synapse: column "
        "names, types, and 2 example rows per view. Use BEFORE writing SQL "
        "whenever you are unsure of exact column names or value formats, and "
        "AFTER any nl2sql error that mentions unknown columns/views."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": (
                    "Optional substring to narrow views, e.g. 'occurrence', "
                    "'amo', 'audit'. Omit for all views."
                ),
            }
        },
        "additionalProperties": False,
    },
    strict=False,
)

# ---------------------------------------------------------------------------
# Create / update agent version
# ---------------------------------------------------------------------------
definition = PromptAgentDefinition(
    model=MODEL_DEPLOYMENT,
    instructions=INSTRUCTIONS,
    tools=[ai_search_tool, nl2sql_tool, chart_tool, dashboard_tool, schema_tool],
)

version = project.agents.create_version(
    agent_name=AGENT_NAME,
    definition=definition,
    description="Safety Intelligence Bot — NL2SQL + doc grounding for the CAAS Safety Regulation warehouse.",
)

print("Created agent version:")
print(f"  agent_name : {AGENT_NAME}")
print(f"  version    : {getattr(version, 'version', '?')}")
print(f"  id         : {getattr(version, 'id', '?')}")
print()
print("Set this on the Container App:")
print(f"  FOUNDRY_AGENT_NAME={AGENT_NAME}")
