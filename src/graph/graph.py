"""AgentCore Platform v1.0"""

# Financial Data Entry Validation Agent (Cat 2, flat composition)
#
# Architecture: FLAT (not nested Cat-2). All domain nodes are registered
# directly in the backbone — no GraphNode wrapper / inner graph.
#
# Backbone (customised via add_edges() override):
#
#   START → initialize → pre_process → main(parse) → [SUCCESS] validate
#                                                   → [ERROR]   finalize
#         validate → generate_report → post_process → finalize → END
#
# Slot mapping:
#   pre_process   = PreProcessNode   (input gate; VERIFIED_EXTERNAL)
#   main          = ParseNode        (satisfies compile() non-None requirement;
#                                    routes to 'validate' on SUCCESS)
#   validate      = ValidateNode     (domain validation rules)
#   generate_report = GenerateReportNode (builds formatted_output)
#   post_process  = PostProcessNode  (output gate)
#
# Class name: FinancialDataEntryValidationAgent — the dotted `class:` entry in
# config/agent.yaml and the import in src/api/server.py must both name it.
#
# Rules:
#   ✅ FinancialDataEntryValidationAgent inherits AgentBaseGraph (framework base class)
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ pre_process / main / post_process all non-None (compile() gate)
#   ✅ add_edges() overridden for flat 5-node linear pipeline
#   ✅ route() inherited from AgentBaseGraph (satisfies ABC; unused in our wiring)
#   ❌ No platform-internal imports
#   ❌ No GraphNode / inner graph (flat, not nested)

from typing import Any

from langgraph.graph import END, START

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.generate_report_node import GenerateReportNode
from src.nodes.parse_node import ParseNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.validate_node import ValidateNode
from src.schemas.state import State


class FinancialDataEntryValidationAgent(AgentBaseGraph):
    """Flat Cat-2 graph for FIN-C2-006 Financial Data Entry Validation.

    Inherits AgentBaseGraph directly (framework base class).
    All domain nodes are wired at the same level via a custom add_edges().

    Pipeline (linear — no retry loop in this flat build):
      START → initialize → pre_process → parse(main) → validate
            → generate_report → post_process → finalize → END

    register_nodes() is the ONLY additional override beyond add_edges().
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "FinancialDataEntryValidationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all backbone slots and extra domain nodes.

        super().register_nodes() MUST be called first — it injects:
          initialize: InitializeNode (sets schema_version, session_id, trust_level)
          finalize:   FinalizeNode   (builds response_metadata, total_time_ms)

        The three required backbone slots (compile() gate) are filled with:
          pre_process  = PreProcessNode   (outer input gate)
          main         = ParseNode        (first domain step after pre_process)
          post_process = PostProcessNode  (outer output gate)

        Extra domain nodes (wired via custom add_edges()):
          validate        = ValidateNode
          generate_report = GenerateReportNode
        """
        super().register_nodes()  # fills: initialize, finalize; sets pre/main/post to None

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ParseNode()
        self._nodes["validate"] = ValidateNode()
        self._nodes["generate_report"] = GenerateReportNode()
        self._nodes["post_process"] = PostProcessNode()

    def add_edges(self) -> None:
        """Wire the flat 5-node domain pipeline.

        Overrides AgentBaseGraph.add_edges() to insert validate and
        generate_report between parse (main) and post_process.

        Routing from parse (main):
          SUCCESS → validate (continue the domain pipeline)
          RETRY   → pre_process (retry loop — max_retry applies)
          other   → finalize (ERROR, TIMEOUT, CANCELLED, ...)

        All other edges are simple (deterministic).
        """
        self._sg.add_edge(START, "initialize")
        self._sg.add_edge("initialize", "pre_process")
        self._sg.add_edge("pre_process", "main")  # main = parse
        self._sg.add_conditional_edges("main", self._route_after_parse)  # SUCCESS → validate
        self._sg.add_edge("validate", "generate_report")
        self._sg.add_edge("generate_report", "post_process")
        self._sg.add_edge("post_process", "finalize")
        self._sg.add_edge("finalize", END)

    def _route_after_parse(self, state: dict[str, Any]) -> str:
        """Route from parse (main slot): SUCCESS → validate, RETRY → pre_process, else → finalize."""
        # State carries the plain status .value string, so route by string
        # comparison (an unknown or missing status falls through to finalize).
        status_val = state.get("status", AgentStatus.PENDING.value)

        if status_val == AgentStatus.SUCCESS.value:
            return "validate"

        retry_count = state.get("retry_count", 0)
        max_retry = self.config.get("max_retry", 3)
        if status_val == AgentStatus.RETRY.value and retry_count < max_retry:
            return "pre_process"

        return "finalize"
