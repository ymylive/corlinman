//! Tracing middleware: inject request_id / traceparent; open subsystem span.
//
// TODO: read `traceparent` header (W3C), generate if absent; bind into
//       `tracing::Span` fields {request_id, subsystem, route, method}.
// TODO: propagate outbound via gRPC metadata so Python structlog sees the same trace id.
