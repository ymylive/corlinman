# corlinman-server

``grpc.aio`` server entrypoint for the corlinman Python AI plane.
Run as a managed subprocess by the Rust gateway — does **not** expose an
HTTP port. On ``SIGTERM`` exits with status 143.
