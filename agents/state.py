# SPDX-License-Identifier: MIT
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

# core.tiers is a stdlib-only leaf (see the header of that module) — importing
# it here cannot create a cycle back through models/ or db/.
from core.tiers import Tier


@dataclass
class AgentState:
    """
    Enterprise Agent State Container

    Used across orchestrator, tools, retriever, and generator.
    Stateless execution model — safe for concurrent execution.
    """

    # ============================================================
    # INPUT
    # ============================================================

    question: str
    repo_filter: Optional[str] = None
    model_hint: Optional[str] = None   # explicit user model selection ("gpt"|"claude"|None=auto)
    # ── Governed routing (Phase 6.5, item 1) ─────────────────────────────────
    # `model_hint` above and `tier` here are NOT two spellings of the same
    # thing, which is why both exist:
    #
    #   model_hint — the USER picked this model from a dropdown. Governance
    #                must not second-guess it, for the same reason an explicit
    #                pick is never overridden anywhere else on the platform.
    #   tier       — the CALLING CODE is declaring what the task needs
    #                ("complex"), and the administrator's tier assignment
    #                decides which model answers.
    #
    # `legacy_hint` is D15's companion to `tier`: the hint this call site
    # passed before it was migrated, used whenever governed resolution produces
    # nothing (flag off, or the tier is unassigned) so the migration is a no-op
    # on a deployment that has not opted in. Deleted in Phase 10 with the rest
    # of the legacy chain.
    tier: Optional[Tier] = None
    legacy_hint: Optional[str] = None
    raw_question: Optional[str] = None  # bare user question — used for compliance scanning (no history injected)
    mode: Optional[str] = None         # UI surface: None/"chat" (default) | "office" (Cowork — enable connectors + office persona in planner)


    # ============================================================
    # PREPROCESSING
    # ============================================================

    rewritten_question: Optional[str] = None
    intent: Optional[str] = None


    # ============================================================
    # RETRIEVAL
    # ============================================================

    context: List[str] = field(default_factory=list)
    retrieval_score: float = 0.0
    retrieval_sources: List[str] = field(default_factory=list)


    # ============================================================
    # GENERATION OUTPUT
    # ============================================================

    answer: str = ""
    tokens_generated: int = 0


    # ============================================================
    # TOOL CONTROL FLAGS
    # ============================================================

    use_retrieve:   bool = True
    use_local_llm:  bool = False
    use_compliance: bool = False


    # ============================================================
    # COMPLIANCE ENGINE (PHASE 4 READY)
    # ============================================================

    compliance_flags: List[str] = field(default_factory=list)
    compliance_score: float = 0.0


    # ============================================================
    # EXECUTION CONTROL
    # ============================================================

    confidence: float = 0.0
    iterations: int = 0
    max_iterations: int = 3


    # ============================================================
    # MULTI-TURN CONVERSATION HISTORY
    # ============================================================

    # Full messages list [{role, content}] from gateway — preserved across model switches.
    # generate_answer_tool uses this for proper multi-turn formatting instead of the
    # flat _question_with_history string so all gateways (cloud + local) get real turns.
    messages: List[dict] = field(default_factory=list)

    # ============================================================
    # USER CONTEXT (RBAC / ABAC)
    # ============================================================

    user_ctx: Optional[Dict[str, Any]] = None   # {user_id, user_role, ad_level, department, is_admin, org_id, session_id}


    # ============================================================
    # DEBUG / TRACE / OBSERVABILITY
    # ============================================================

    tool_history: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ReAct scratchpad — Claude's reasoning trace for this request
    # Each entry: {"step": int, "thought": str, "tool": str, "observation": str}
    scratchpad: List[Dict[str, Any]] = field(default_factory=list)


    # ============================================================
    # HELPER METHODS (ENTERPRISE-SAFE)
    # ============================================================

    def add_tool(self, tool_name: str):
        self.tool_history.append(tool_name)


    def add_error(self, error: str):
        self.errors.append(error)


    def increment_iteration(self):
        self.iterations += 1


    def should_continue(self):
        return self.iterations < self.max_iterations


# from dataclasses import dataclass, field
# from typing import List, Optional
#
#
# @dataclass
# class AgentState:
#
#     question: str
#
#     repo_filter: Optional[str] = None
#
#     intent: Optional[str] = None
#
#     context: List[str] = field(default_factory=list)
#
#     compliance_flags: List[str] = field(default_factory=list)
#
#     answer: Optional[str] = None
#
#     confidence: float = 0.0
#
#     use_local_llm: bool = False
#
#     use_retrieval: bool = True
#
#     use_compliance: bool = True