//! MCP runtime sub-modules.
//!
//! Today this is just `redact` (env-passthrough filtering + log
//! redaction). Subsequent iters will add `adapter`, `multiplex`, etc.

pub mod redact;
