"""EvolutionEngine — signals → clustering → kind-handler proposals."""

from corlinman_evolution_engine.agent_card import KIND_AGENT_CARD, AgentCardHandler
from corlinman_evolution_engine.clustering import SignalCluster, cluster_signals
from corlinman_evolution_engine.consolidation import (
    CONSOLIDATED_NAMESPACE,
    ConsolidationConfig,
    ConsolidationSummary,
    consolidation_run_once,
)
from corlinman_evolution_engine.engine import (
    DEFAULT_HANDLERS,
    EngineConfig,
    EvolutionEngine,
    RunSummary,
)
from corlinman_evolution_engine.memory_op import (
    KIND_MEMORY_OP,
    DuplicatePair,
    MemoryOpHandler,
    find_near_duplicate_pairs,
    jaccard,
)
from corlinman_evolution_engine.prompt_template import (
    KIND_PROMPT_TEMPLATE,
    PromptTemplateHandler,
)
from corlinman_evolution_engine.proposals import (
    EvolutionProposal,
    KindHandler,
    ProposalContext,
    format_day_prefix,
    mint_proposal_id,
)
from corlinman_evolution_engine.skill_update import (
    KIND_SKILL_UPDATE,
    SkillUpdateHandler,
)
from corlinman_evolution_engine.store import DEFAULT_TENANT_ID
from corlinman_evolution_engine.tag_rebalance import (
    KIND_TAG_REBALANCE,
    TagRebalanceHandler,
)
from corlinman_evolution_engine.tool_policy import (
    KIND_TOOL_POLICY,
    ToolPolicyHandler,
)

__all__ = [
    "CONSOLIDATED_NAMESPACE",
    "DEFAULT_HANDLERS",
    "DEFAULT_TENANT_ID",
    "KIND_AGENT_CARD",
    "KIND_MEMORY_OP",
    "KIND_PROMPT_TEMPLATE",
    "KIND_SKILL_UPDATE",
    "KIND_TAG_REBALANCE",
    "KIND_TOOL_POLICY",
    "AgentCardHandler",
    "ConsolidationConfig",
    "ConsolidationSummary",
    "DuplicatePair",
    "EngineConfig",
    "EvolutionEngine",
    "EvolutionProposal",
    "KindHandler",
    "MemoryOpHandler",
    "PromptTemplateHandler",
    "ProposalContext",
    "RunSummary",
    "SignalCluster",
    "SkillUpdateHandler",
    "TagRebalanceHandler",
    "ToolPolicyHandler",
    "cluster_signals",
    "consolidation_run_once",
    "find_near_duplicate_pairs",
    "format_day_prefix",
    "jaccard",
    "mint_proposal_id",
]
