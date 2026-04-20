"""Local embedding pool — ``ProcessPoolExecutor`` around sentence-transformers.

Responsibility: run sentence-transformers in a process pool (not thread pool)
so Torch's BLAS threads and the Python GIL don't block the asyncio event
loop on the main process. Model is loaded lazily per worker.

Requires the ``[local]`` extra (``sentence-transformers``).

TODO(M4): implement worker init (load model once), batch coalescing, and
``asyncio.run_in_executor`` wrapper.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)
