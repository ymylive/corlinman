# corlinman-embedding

Embedding router. Dispatches to either a local ``sentence-transformers``
``ProcessPoolExecutor`` (to side-step the GIL) or to a remote provider
over HTTP. The ``[local]`` extra pulls in ``sentence-transformers`` +
``torch``; the default install stays slim for the ``corlinman:1.0.0``
image (see plan §10 / R7).
