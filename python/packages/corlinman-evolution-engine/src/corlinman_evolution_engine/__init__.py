"""Phase 2 EvolutionEngine — signals → clustering → memory_op proposals."""

from corlinman_evolution_engine.clustering import SignalCluster, cluster_signals
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

__all__ = [
    "DEFAULT_HANDLERS",
    "KIND_MEMORY_OP",
    "DuplicatePair",
    "EngineConfig",
    "EvolutionEngine",
    "EvolutionProposal",
    "KindHandler",
    "MemoryOpHandler",
    "ProposalContext",
    "RunSummary",
    "SignalCluster",
    "cluster_signals",
    "find_near_duplicate_pairs",
    "format_day_prefix",
    "jaccard",
    "mint_proposal_id",
]
