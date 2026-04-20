//! Cancellation helpers: `combine(tokens)` and `with_timeout(fut, millis)`.
//
// TODO: port openclaw's `combineAbortSignals` — return a `CancellationToken`
//       that fires when any parent token fires; dropping the handle unsubscribes.
// TODO: `with_timeout` must map elapsed → `CorlinmanError::Timeout { what, millis }`
//       (not a bare `Elapsed`), so the edge layer renders a clean 504.
