"""Stable retrieval (R) interface for the KB's (Update, Database, Retrieve) design.

MemoryRetriever owns the read paths and scorer selection; memory_index remains
the implementation. The default instance reads the live files and environment
at call time, so long-running callers see nightly rebuilds and tuning changes.
No index/cache builds or writes occur here. Update/consolidation stays in kb.py.

Configured instances can coexist without patching module globals or os.environ.
For an isolated KB, supply all three paths (including its embedding cache).
Backend service settings and embedding/rerank knobs still use the existing env
variables; this facade does not change their configuration contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import memory_index


@dataclass(frozen=True)
class MemoryRetriever:
    """Read-only retrieval over an index, with optional instance configuration.

    None paths use the live defaults. Missing explicit paths never fall back to
    live files: index -> no matches, tuning -> DEFAULT_TUNING, embeddings ->
    lexical scoring. A malformed index still raises for the caller to log.
    Tuning dicts are complete configs (as with memory_index.retrieve); precedence
    is per-call tuning > instance tuning > tuning file merged over defaults.
    None mode reads KB_RETRIEVAL on each call; explicit modes bypass that env var.
    """

    index_path: Path | None = None
    tuning_path: Path | None = None
    embedding_path: Path | None = None
    tuning: dict | None = None
    mode: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in (None, 'lexical', 'embedding', 'hybrid', 'rerank'):
            raise ValueError(f'Unknown retrieval mode: {self.mode}')

    def retrieve(self, query: str, project: str | None = None, max_facts: int = 6,
                 tuning: dict | None = None, limit: int = 50) -> dict:
        """Return query/project, scorer provenance, top_tier and ranked matches.

        Matches retain slug/display/score/tier/projects/key_points/path. Project
        filtering, fact limits, demotions and fallback provenance are unchanged.
        project=None retains the broad legacy search; MCP applies global-only
        filtering separately. Callers decide whether to inject or advertise.
        """
        return memory_index.retrieve(
            query, project=project, max_facts=max_facts,
            tuning=self.tuning if tuning is None else tuning, limit=limit,
            index_path=self.index_path, tuning_path=self.tuning_path,
            embedding_path=self.embedding_path, mode=self.mode,
        )


# Same call signature as MemoryRetriever.retrieve. Consumers may replace this
# callable with a configured instance's retrieve method or another implementation.
retrieve = MemoryRetriever().retrieve
