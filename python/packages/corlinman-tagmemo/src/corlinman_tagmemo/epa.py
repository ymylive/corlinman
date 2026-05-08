"""Energy-Perception-Action basis + projection.

Given a set of chunk embedding vectors, cluster them, center by weighted mean,
and SVD the (K, d) centroid matrix to obtain an ortho-basis sorted by energy.
Queries are projected onto this basis and scored for dominance / entropy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sklearn.cluster import KMeans


@dataclass(frozen=True)
class DominantAxis:
    label: str
    energy: float
    projection: float


@dataclass(frozen=True)
class EpaBasis:
    ortho_basis: np.ndarray  # (K, d) rows are unit-norm
    basis_mean: np.ndarray  # (d,)
    basis_energies: np.ndarray  # (K,) nonneg, descending
    basis_labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EpaProjection:
    projections: np.ndarray  # (K,)
    probabilities: np.ndarray  # (K,) softmax(|proj|*energy)
    entropy: float  # normalized Shannon entropy in [0,1]
    logic_depth: float  # 1 - entropy
    dominant_axes: list[DominantAxis]  # top-3 sorted by |proj*energy|


def _stable_softmax(scores: np.ndarray) -> np.ndarray:
    if scores.size == 0:
        return scores
    shifted = scores - np.max(scores)
    exp = np.exp(shifted)
    denom = float(np.sum(exp))
    if denom <= 0.0 or not np.isfinite(denom):
        # Degenerate fallback: uniform distribution.
        return np.full_like(scores, 1.0 / scores.size)
    return np.asarray(exp / denom, dtype=np.float64)


def fit_basis(
    vectors: np.ndarray,
    weights: np.ndarray | None = None,
    k: int = 8,
    labels: list[str] | None = None,
) -> EpaBasis:
    """Cluster + SVD an (n_chunks, d) matrix into an EPA ortho-basis."""
    if vectors.ndim != 2:
        raise ValueError(f"vectors must be 2-D, got shape {vectors.shape!r}")
    n_chunks, _d = vectors.shape
    if n_chunks == 0:
        raise ValueError("vectors must contain at least one row")
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    # KMeans requires n_clusters <= n_samples.
    effective_k = min(k, n_chunks)

    if weights is None:
        weights = np.ones(n_chunks, dtype=np.float64)
    else:
        weights = np.asarray(weights, dtype=np.float64)
        if weights.shape != (n_chunks,):
            raise ValueError(
                f"weights must have shape ({n_chunks},), got {weights.shape!r}"
            )
        if np.any(weights < 0):
            raise ValueError("weights must be non-negative")

    vectors = np.asarray(vectors, dtype=np.float64)

    km = KMeans(n_clusters=effective_k, random_state=42, n_init=10)
    # sklearn expects sample_weight for weighted centroid computation.
    assignments = km.fit_predict(vectors, sample_weight=weights)
    centroids = km.cluster_centers_  # (effective_k, d)

    # Cluster weight = sum of sample weights in that cluster.
    cluster_weights = np.zeros(effective_k, dtype=np.float64)
    for i in range(effective_k):
        cluster_weights[i] = float(np.sum(weights[assignments == i]))

    total_weight = float(np.sum(cluster_weights))
    if total_weight <= 0.0:
        raise ValueError("total cluster weight is zero; cannot form basis")

    # Weighted mean of centroids (weighted by cluster size).
    basis_mean = np.average(centroids, axis=0, weights=cluster_weights)

    centered = centroids - basis_mean  # (effective_k, d)
    # Row-wise weight by sqrt(cluster_weight_k).
    row_scale = np.sqrt(cluster_weights)[:, None]
    weighted = centered * row_scale  # (effective_k, d)

    # SVD: Vt rows are orthonormal axes in d-space.
    _, singular_values, vt = np.linalg.svd(weighted, full_matrices=False)
    # Already sorted descending. basis_energies = S^2.
    energies = (singular_values**2).astype(np.float64)
    axes = vt.astype(np.float64)  # (min(effective_k, d), d)

    # Pad / trim to exactly effective_k axes. If d < effective_k (tiny dim
    # case), SVD returns fewer rows; keep what we have.
    kept = min(effective_k, axes.shape[0])
    axes = axes[:kept]
    energies = energies[:kept]

    if labels is not None:
        if len(labels) != kept:
            raise ValueError(
                f"labels must have length {kept} (effective K after SVD), "
                f"got {len(labels)}"
            )
        final_labels = list(labels)
    else:
        final_labels = [f"axis_{i}" for i in range(kept)]

    return EpaBasis(
        ortho_basis=axes,
        basis_mean=basis_mean.astype(np.float64),
        basis_energies=energies,
        basis_labels=final_labels,
    )


def project(basis: EpaBasis, query_vec: np.ndarray) -> EpaProjection:
    """Project a query vector onto the basis and compute dominance stats."""
    query_vec = np.asarray(query_vec, dtype=np.float64).reshape(-1)
    if query_vec.shape[0] != basis.basis_mean.shape[0]:
        raise ValueError(
            f"query dim {query_vec.shape[0]} != basis dim "
            f"{basis.basis_mean.shape[0]}"
        )

    centered = query_vec - basis.basis_mean
    # (K, d) @ (d,) -> (K,)
    projections = basis.ortho_basis @ centered

    scores = np.abs(projections) * basis.basis_energies
    probabilities = _stable_softmax(scores)

    k = probabilities.shape[0]
    if k <= 1:
        entropy_norm = 0.0
    else:
        # Shannon entropy, natural log, guarded against log(0).
        p_safe = np.clip(probabilities, 1e-12, 1.0)
        h = float(-np.sum(probabilities * np.log(p_safe)))
        entropy_norm = float(h / np.log(k))
        # Floating-point cleanup.
        entropy_norm = float(np.clip(entropy_norm, 0.0, 1.0))

    logic_depth = 1.0 - entropy_norm

    # Dominant axes: top-3 by |proj * energy|.
    contribution = np.abs(projections) * basis.basis_energies
    order = np.argsort(-contribution)
    top = order[: min(3, k)]
    dominant = [
        DominantAxis(
            label=basis.basis_labels[int(i)],
            energy=float(basis.basis_energies[int(i)]),
            projection=float(projections[int(i)]),
        )
        for i in top
    ]

    return EpaProjection(
        projections=projections.astype(np.float64),
        probabilities=probabilities.astype(np.float64),
        entropy=entropy_norm,
        logic_depth=logic_depth,
        dominant_axes=dominant,
    )
