//! Auth middleware: API_Key (Bearer) for `/v1/*` and AdminUsername/Password for `/admin/*`.
//
// TODO: Bearer token auth (read from config.toml [admin]); reject with 401 + ErrorInfo on miss.
// TODO: `/admin/*` uses HTTP Basic against `AdminUsername`/`AdminPassword` + session cookie.
