"""``python -m corlinman_server`` entrypoint — delegates to ``main.main``."""

from __future__ import annotations

from corlinman_server.main import main

if __name__ == "__main__":  # pragma: no cover — module entrypoint
    main()
