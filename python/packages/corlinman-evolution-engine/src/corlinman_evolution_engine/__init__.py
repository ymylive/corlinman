"""EvolutionEngine — signals → clustering → kind-handler proposals."""

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
from corlinman_evolution_engine.tag_rebalance import (
    KIND_TAG_REBALANCE,
    TagRebalanceHandler,
)

__all__ = [
    "CONSOLIDATED_NAMESPACE",
    "DEFAULT_HANDLERS",
    "KIND_MEMORY_OP",
    "KIND_SKILL_UPDATE",
    "KIND_TAG_REBALANCE",
    "ConsolidationConfig",
    "ConsolidationSummary",
    "DuplicatePair",
    "EngineConfig",
    "EvolutionEngine",
    "EvolutionProposal",
    "KindHandler",
    "MemoryOpHandler",
    "ProposalContext",
    "RunSummary",
    "SignalCluster",
    "SkillUpdateHandler",
    "TagRebalanceHandler",
    "cluster_signals",
    "consolidation_run_once",
    "find_near_duplicate_pairs",
    "format_day_prefix",
    "jaccard",
    "mint_proposal_id",
]
