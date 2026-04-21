//! NapCat webui proxy + account history for QQ scan-login.
//!
//! This module powers the four admin routes mounted by
//! [`super::channels::router`]:
//!
//! - `POST /admin/channels/qq/qrcode`        — request a fresh QR from NapCat.
//! - `GET  /admin/channels/qq/qrcode/status` — poll NapCat for login status.
//! - `GET  /admin/channels/qq/accounts`      — list previously-seen accounts
//!   from `<data_dir>/qq-accounts.json`.
//! - `POST /admin/channels/qq/quick-login`   — ask NapCat to reuse a stored
//!   session for a given uin.
//!
//! ### NapCat API version assumed
//! We target the **NapCat webui** HTTP API (NapCatQQ v2.x — the `/api/*`
//! surface exposed by the embedded webserver, separate from the OneBot-v11
//! ws/ho endpoint on port 3001). Paths + Bearer auth match the webui:
//!
//! - `POST /api/QQLogin/GetQQLoginQrcode`   → `{ code, data: { qrcode, ...} }`
//!   where `qrcode` is either a base64 PNG ("iVBORw0KGg…") *or* an
//!   `ptqrshow` URL; we forward both forms to the UI untouched.
//! - `POST /api/QQLogin/CheckLoginStatus`   → `{ code, data: { isLogin,
//!   qrcodeurl, uin?, nick?, avatarUrl? } }`.
//! - `POST /api/QQLogin/GetQuickLoginList`  → `{ code, data: [{uin, nickName,
//!   isQuickLogin, ...}] }`.
//! - `POST /api/QQLogin/SetQuickLogin`      → `{ code, data: {...} }`.
//!
//! openclaw's `qq_login.html` hits `/admin_api/napcat/*` paths which are
//! served by a proxy fronting the same endpoints; since corlinman is the
//! proxy, we call NapCat directly on its webui port (default 6099).
//!
//! ### Account history
//! Every time CheckLoginStatus reports `isLogin = true` we upsert the
//! `{uin, nickname, avatar_url, last_login_at}` row into
//! `$CORLINMAN_DATA_DIR/qq-accounts.json` via an atomic tmp-then-rename
//! write. The list is also surfaced by `/accounts` for quick-login UX.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::Mutex;

use super::AdminState;

/// HTTP timeout for NapCat calls. NapCat's `GetQQLoginQrcode` occasionally
/// takes ~2s when it re-requests a token from Tencent, so keep the ceiling
/// generous but bounded.
const NAPCAT_TIMEOUT: Duration = Duration::from_secs(6);

/// Filename inside `<data_dir>/` for the account history JSON.
pub const ACCOUNTS_FILE: &str = "qq-accounts.json";

// ---------------------------------------------------------------------------
// Public handlers — each returns an `impl IntoResponse`.
// ---------------------------------------------------------------------------

pub async fn qrcode(State(state): State<AdminState>) -> Response {
    let ctx = match NapcatContext::from_state(&state) {
        Ok(c) => c,
        Err(resp) => return resp,
    };
    match ctx.request_qrcode().await {
        Ok(out) => Json(out).into_response(),
        Err(err) => err.into_response(),
    }
}

#[derive(Debug, Deserialize)]
pub struct StatusQuery {
    pub token: String,
}

pub async fn qrcode_status(
    State(state): State<AdminState>,
    Query(q): Query<StatusQuery>,
) -> Response {
    let ctx = match NapcatContext::from_state(&state) {
        Ok(c) => c,
        Err(resp) => return resp,
    };
    match ctx.check_status(&q.token).await {
        Ok(out) => Json(out).into_response(),
        Err(err) => err.into_response(),
    }
}

pub async fn accounts(State(state): State<AdminState>) -> Response {
    let path = accounts_path(&state);
    match load_accounts(&path).await {
        Ok(list) => Json(AccountsOut { accounts: list }).into_response(),
        Err(err) => internal(format!("failed to read {}: {err}", path.display())),
    }
}

#[derive(Debug, Deserialize)]
pub struct QuickLoginBody {
    pub uin: String,
}

pub async fn quick_login(
    State(state): State<AdminState>,
    Json(body): Json<QuickLoginBody>,
) -> Response {
    if body.uin.trim().is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": "invalid_uin", "message": "uin is required"})),
        )
            .into_response();
    }
    let ctx = match NapcatContext::from_state(&state) {
        Ok(c) => c,
        Err(resp) => return resp,
    };
    let path = accounts_path(&state);
    match ctx.quick_login(&body.uin).await {
        Ok(out) => {
            if let Some(acct) = out.account.clone() {
                if let Err(err) = upsert_account(&path, &acct).await {
                    tracing::warn!(error = %err, path = %path.display(),
                        "qq-accounts: upsert after quick-login failed");
                }
            }
            Json(out).into_response()
        }
        Err(err) => err.into_response(),
    }
}

// ---------------------------------------------------------------------------
// NapCat context + client
// ---------------------------------------------------------------------------

struct NapcatContext {
    url: String,
    access_token: Option<String>,
    accounts_path: PathBuf,
    client: Client,
}

impl NapcatContext {
    // `Response` is ~128 bytes; keeping it inline avoids a box on the
    // happy path that dominates call frequency. The large-err lint is
    // noise in this context.
    #[allow(clippy::result_large_err)]
    fn from_state(state: &AdminState) -> Result<Self, Response> {
        let cfg = state.config.load_full();
        let qq = match cfg.channels.qq.as_ref() {
            Some(q) => q,
            None => {
                return Err(service_unavailable(
                    "channel_not_configured",
                    "no [channels.qq] section in config",
                ))
            }
        };
        // Resolution order:
        //   1. `[channels.qq].napcat_url` in config.toml (explicit)
        //   2. `$CORLINMAN_NAPCAT_URL` env (container defaults, e.g. the
        //      docker-compose.qq.yml profile sets this to http://napcat:6099
        //      so operators don't have to edit config.toml to enable the
        //      scan-login flow)
        //   3. 503 `napcat_not_configured`
        let url = qq
            .napcat_url
            .as_ref()
            .filter(|u| !u.trim().is_empty())
            .cloned()
            .or_else(|| std::env::var("CORLINMAN_NAPCAT_URL").ok())
            .map(|u| u.trim_end_matches('/').to_string());
        let url = match url {
            Some(u) if !u.is_empty() => u,
            _ => return Err(service_unavailable(
                "napcat_not_configured",
                "[channels.qq].napcat_url is empty; set it in config.toml or export CORLINMAN_NAPCAT_URL (e.g. http://127.0.0.1:6099) to enable scan-login",
            )),
        };
        let access_token = match qq.napcat_access_token.as_ref() {
            Some(sec) => match sec.resolve() {
                Ok(v) => Some(v),
                Err(err) => return Err(internal(format!("napcat_access_token: {err}"))),
            },
            None => None,
        };
        let client = Client::builder()
            .timeout(NAPCAT_TIMEOUT)
            .build()
            .unwrap_or_else(|_| Client::new());
        Ok(Self {
            url,
            access_token,
            accounts_path: accounts_path(state),
            client,
        })
    }

    async fn request_qrcode(&self) -> Result<QrcodeOut, NapcatError> {
        let body = self
            .post("/api/QQLogin/GetQQLoginQrcode", json!({}))
            .await?;
        let data = extract_ok_data(&body)?;
        let qrcode = data
            .get("qrcode")
            .and_then(Value::as_str)
            .ok_or_else(|| NapcatError::bad_response("missing data.qrcode"))?
            .to_string();
        let (image_base64, qrcode_url) = classify_qrcode(&qrcode);
        // Token is just a correlation id we use in `/qrcode/status`; NapCat
        // re-derives the QR state from its in-memory session, so we don't
        // need to pass it through. A uuid is plenty.
        let token = uuid::Uuid::new_v4().to_string();
        let expires_at_ms = now_ms().saturating_add(
            // NapCat's QR is valid ~120s per openclaw's EXPIRE_MS. If
            // NapCat ships an `expire_in_seconds` field in the future
            // we'd prefer that; for now mirror the html.
            120_000,
        );
        Ok(QrcodeOut {
            token,
            image_base64,
            qrcode_url,
            expires_at: expires_at_ms,
        })
    }

    async fn check_status(&self, _token: &str) -> Result<StatusOut, NapcatError> {
        // NapCat stores the pending QR in its own process; the `token` we
        // issued is a correlation id only (see `request_qrcode`). We still
        // forward it back so the UI can sanity-check which QR it's polling.
        let body = self
            .post("/api/QQLogin/CheckLoginStatus", json!({}))
            .await?;
        let data = extract_ok_data(&body)?;
        let is_login = data
            .get("isLogin")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if is_login {
            let account = parse_account(data);
            if let Some(acct) = account.as_ref() {
                if let Err(err) = upsert_account(&self.accounts_path, acct).await {
                    tracing::warn!(error = %err, path = %self.accounts_path.display(),
                        "qq-accounts: upsert after confirm failed");
                }
            }
            return Ok(StatusOut {
                status: "confirmed".into(),
                account,
                message: None,
            });
        }
        // Not yet logged in. NapCat doesn't publish a "scanned" bit via
        // this endpoint — the phone side is fire-and-forget until the user
        // confirms. Treat empty qrcodeurl as expired; anything else as
        // still-waiting.
        let qr_url = data.get("qrcodeurl").and_then(Value::as_str).unwrap_or("");
        let status = if qr_url.is_empty() {
            "expired"
        } else {
            "waiting"
        };
        Ok(StatusOut {
            status: status.into(),
            account: None,
            message: None,
        })
    }

    async fn quick_login(&self, uin: &str) -> Result<StatusOut, NapcatError> {
        let body = self
            .post("/api/QQLogin/SetQuickLogin", json!({ "uin": uin }))
            .await?;
        let data = extract_ok_data(&body)?;
        let is_login = data.get("isLogin").and_then(Value::as_bool).unwrap_or(true); // NapCat returns just {result:true} sometimes.
        let account = parse_account(data).or_else(|| {
            Some(QqAccount {
                uin: uin.to_string(),
                nickname: None,
                avatar_url: None,
                last_login_at: now_ms(),
            })
        });
        Ok(StatusOut {
            status: if is_login { "confirmed" } else { "error" }.into(),
            account,
            message: None,
        })
    }

    async fn post(&self, path: &str, body: Value) -> Result<Value, NapcatError> {
        // NapCat v2 webui wraps every protected endpoint in a JWT-ish
        // `Credential` obtained by POST /api/auth/login { hash: sha256(token+".napcat") }.
        // Bearer with the raw token just returns Unauthorized. Login lazily
        // per call — NapCat's credentials expire after ~1h so caching adds
        // fragility for negligible throughput gain on an admin surface.
        let credential = match self.access_token.as_deref() {
            Some(t) if !t.is_empty() => Some(self.login_credential(t).await?),
            _ => None,
        };
        let mut req = self
            .client
            .post(format!("{}{}", self.url, path))
            .json(&body);
        if let Some(c) = credential.as_deref() {
            req = req.bearer_auth(c);
        }
        let resp = req.send().await.map_err(NapcatError::transport)?;
        if !resp.status().is_success() {
            let code = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(NapcatError::upstream(code, text));
        }
        resp.json::<Value>().await.map_err(NapcatError::decode)
    }

    /// Exchange the raw admin-panel token for a short-lived Credential per
    /// the NapCat webui auth flow:
    ///
    ///     POST /api/auth/login { "hash": sha256(token + ".napcat") }
    ///     → { code: 0, data: { Credential: <base64-json> } }
    async fn login_credential(&self, token: &str) -> Result<String, NapcatError> {
        use sha2::{Digest, Sha256};
        let mut h = Sha256::new();
        h.update(token.as_bytes());
        h.update(b".napcat");
        let hash = format!("{:x}", h.finalize());
        let url = format!("{}/api/auth/login", self.url);
        let resp = self
            .client
            .post(&url)
            .json(&json!({ "hash": hash }))
            .send()
            .await
            .map_err(NapcatError::transport)?;
        if !resp.status().is_success() {
            let code = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(NapcatError::upstream(code, text));
        }
        let body: Value = resp.json().await.map_err(NapcatError::decode)?;
        let data = extract_ok_data(&body)?;
        data.get("Credential")
            .and_then(Value::as_str)
            .map(|s| s.to_string())
            .ok_or_else(|| NapcatError::bad_response("missing data.Credential"))
    }
}

/// NapCat's envelope is `{ code: 0, data: {...}, message?: string }`. A
/// non-zero `code` is an application-level failure.
fn extract_ok_data(body: &Value) -> Result<&Value, NapcatError> {
    let code = body.get("code").and_then(Value::as_i64).unwrap_or(-1);
    if code != 0 {
        let msg = body
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("napcat returned a non-zero code")
            .to_string();
        return Err(NapcatError::AppError { code, message: msg });
    }
    body.get("data")
        .ok_or_else(|| NapcatError::bad_response("missing data field"))
}

fn parse_account(data: &Value) -> Option<QqAccount> {
    let uin = data.get("uin").and_then(|v| {
        v.as_str()
            .map(String::from)
            .or_else(|| v.as_i64().map(|n| n.to_string()))
    })?;
    let nickname = data
        .get("nick")
        .or_else(|| data.get("nickName"))
        .and_then(Value::as_str)
        .map(String::from);
    let avatar_url = data
        .get("avatarUrl")
        .or_else(|| data.get("avatar"))
        .and_then(Value::as_str)
        .map(String::from);
    Some(QqAccount {
        uin,
        nickname,
        avatar_url,
        last_login_at: now_ms(),
    })
}

/// NapCat returns either a raw base64 PNG *or* a `ptqrshow` URL for the
/// QR image. We report whichever we got so the UI can pick a renderer;
/// exactly one of the two is `Some`.
fn classify_qrcode(qrcode: &str) -> (Option<String>, Option<String>) {
    let trimmed = qrcode.trim();
    if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
        (None, Some(trimmed.to_string()))
    } else {
        // Drop `data:image/png;base64,` prefix if present; the UI will
        // re-attach its own when rendering an <img src>.
        let bare = trimmed
            .strip_prefix("data:image/png;base64,")
            .or_else(|| trimmed.strip_prefix("data:image/jpeg;base64,"))
            .unwrap_or(trimmed);
        (Some(bare.to_string()), None)
    }
}

// ---------------------------------------------------------------------------
// Wire DTOs
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QrcodeOut {
    pub token: String,
    /// Base64-encoded PNG (no `data:` prefix). Set when NapCat returned a
    /// bare image; mutually exclusive with [`Self::qrcode_url`].
    pub image_base64: Option<String>,
    /// URL to encode as QR client-side. Set when NapCat returned a
    /// `ptqrshow` URL instead of an image.
    pub qrcode_url: Option<String>,
    /// Epoch-ms deadline after which the QR stops being valid on NapCat.
    pub expires_at: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusOut {
    /// One of: `waiting`, `scanned`, `confirmed`, `expired`, `error`.
    /// NapCat's CheckLoginStatus doesn't expose `scanned` today so the
    /// UI will see `waiting` until the user confirms on-phone.
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub account: Option<QqAccount>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct QqAccount {
    pub uin: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nickname: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub avatar_url: Option<String>,
    /// Epoch-ms of the last successful login. Written by every confirm
    /// (scan or quick-login).
    pub last_login_at: u64,
}

#[derive(Debug, Serialize)]
struct AccountsOut {
    accounts: Vec<QqAccount>,
}

// ---------------------------------------------------------------------------
// Accounts file — atomic read / write
// ---------------------------------------------------------------------------

fn accounts_path(state: &AdminState) -> PathBuf {
    state.config.load().server.data_dir.join(ACCOUNTS_FILE)
}

/// Single-process mutex around the accounts file so two concurrent
/// confirms / quick-logins don't stomp each other's rewrite.
static ACCOUNTS_LOCK: once_cell::sync::Lazy<Arc<Mutex<()>>> =
    once_cell::sync::Lazy::new(|| Arc::new(Mutex::new(())));

pub async fn load_accounts(path: &Path) -> std::io::Result<Vec<QqAccount>> {
    match tokio::fs::read(path).await {
        Ok(bytes) => {
            let list: Vec<QqAccount> = serde_json::from_slice(&bytes).unwrap_or_default();
            Ok(list)
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(err) => Err(err),
    }
}

pub async fn upsert_account(path: &Path, acct: &QqAccount) -> std::io::Result<()> {
    let _guard = ACCOUNTS_LOCK.lock().await;
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let mut list = load_accounts(path).await?;
    if let Some(existing) = list.iter_mut().find(|a| a.uin == acct.uin) {
        existing.nickname = acct.nickname.clone().or_else(|| existing.nickname.clone());
        existing.avatar_url = acct
            .avatar_url
            .clone()
            .or_else(|| existing.avatar_url.clone());
        existing.last_login_at = acct.last_login_at;
    } else {
        list.push(acct.clone());
    }
    // Most-recent first so the UI renders without re-sorting.
    list.sort_by_key(|a| std::cmp::Reverse(a.last_login_at));
    let serialised = serde_json::to_vec_pretty(&list)?;
    let mut tmp = path.to_path_buf();
    tmp.as_mut_os_string().push(".new");
    tokio::fs::write(&tmp, &serialised).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Error + response helpers
// ---------------------------------------------------------------------------

#[derive(Debug)]
enum NapcatError {
    Transport(String),
    Upstream { status: StatusCode, body: String },
    AppError { code: i64, message: String },
    Decode(String),
}

impl NapcatError {
    fn transport(err: reqwest::Error) -> Self {
        Self::Transport(err.to_string())
    }
    fn upstream(status: reqwest::StatusCode, body: String) -> Self {
        Self::Upstream {
            status: StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
            body,
        }
    }
    fn decode(err: reqwest::Error) -> Self {
        Self::Decode(err.to_string())
    }
    fn bad_response(msg: &str) -> Self {
        Self::Decode(msg.to_string())
    }
}

impl IntoResponse for NapcatError {
    fn into_response(self) -> Response {
        match self {
            Self::Transport(msg) => (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({
                    "error": "napcat_unavailable",
                    "message": msg,
                })),
            )
                .into_response(),
            Self::Upstream { status, body } => (
                // Bubble NapCat's status up as a 502 (bad gateway) unless it
                // was already a 5xx we want to preserve for the UI toast.
                if status.is_server_error() {
                    status
                } else {
                    StatusCode::BAD_GATEWAY
                },
                Json(json!({
                    "error": "napcat_upstream_error",
                    "status": status.as_u16(),
                    "message": body,
                })),
            )
                .into_response(),
            Self::AppError { code, message } => (
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "error": "napcat_app_error",
                    "code": code,
                    "message": message,
                })),
            )
                .into_response(),
            Self::Decode(msg) => (
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "error": "napcat_decode_failed",
                    "message": msg,
                })),
            )
                .into_response(),
        }
    }
}

fn service_unavailable(code: &'static str, msg: &'static str) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": code, "message": msg})),
    )
        .into_response()
}

fn internal(msg: String) -> Response {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({"error": "internal_error", "message": msg})),
    )
        .into_response()
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use arc_swap::ArcSwap;
    use axum::body::{to_bytes, Body};
    use axum::http::Request;
    use axum::routing::{get, post};
    use axum::Router;
    use corlinman_core::config::{Config, QqChannelConfig, QqRateLimit};
    use corlinman_plugins::registry::PluginRegistry;
    use std::collections::HashMap;
    use std::net::SocketAddr;
    use std::sync::Arc;
    use tempfile::TempDir;
    use tokio::net::TcpListener;
    use tower::ServiceExt;

    /// Tiny axum-backed NapCat stub. `script` decides what `/api/*` returns.
    async fn spawn_mock_napcat(
        qrcode_body: Value,
        status_body: Arc<tokio::sync::Mutex<Value>>,
        quick_body: Value,
    ) -> String {
        let app = Router::new()
            .route(
                "/api/QQLogin/GetQQLoginQrcode",
                post({
                    let body = qrcode_body.clone();
                    move || {
                        let b = body.clone();
                        async move { Json(b) }
                    }
                }),
            )
            .route(
                "/api/QQLogin/CheckLoginStatus",
                post({
                    let body = status_body.clone();
                    move || {
                        let b = body.clone();
                        async move {
                            let v = b.lock().await.clone();
                            Json(v)
                        }
                    }
                }),
            )
            .route(
                "/api/QQLogin/SetQuickLogin",
                post({
                    let body = quick_body.clone();
                    move || {
                        let b = body.clone();
                        async move { Json(b) }
                    }
                }),
            )
            .route("/health", get(|| async { "ok" }));

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr: SocketAddr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });
        format!("http://{addr}")
    }

    fn state_with_napcat(napcat_url: Option<String>, data_dir: PathBuf) -> AdminState {
        let mut cfg = Config::default();
        cfg.server.data_dir = data_dir;
        cfg.channels.qq = Some(QqChannelConfig {
            enabled: true,
            ws_url: "ws://127.0.0.1:3001".into(),
            access_token: None,
            self_ids: vec![42],
            group_keywords: HashMap::new(),
            rate_limit: QqRateLimit::default(),
            napcat_url,
            napcat_access_token: None,
        });
        AdminState::new(
            Arc::new(PluginRegistry::default()),
            Arc::new(ArcSwap::from_pointee(cfg)),
        )
    }

    async fn body_json(resp: Response) -> Value {
        let b = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        serde_json::from_slice(&b).unwrap()
    }

    fn app_with(state: AdminState) -> Router {
        // Mount directly — avoids pulling the auth layer in a unit test.
        Router::new()
            .route("/admin/channels/qq/qrcode", post(qrcode))
            .route("/admin/channels/qq/qrcode/status", get(qrcode_status))
            .route("/admin/channels/qq/accounts", get(accounts))
            .route("/admin/channels/qq/quick-login", post(quick_login))
            .with_state(state)
    }

    #[tokio::test]
    async fn qrcode_returns_503_when_napcat_url_missing() {
        let tmp = TempDir::new().unwrap();
        let state = state_with_napcat(None, tmp.path().to_path_buf());
        let app = app_with(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/qrcode")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        let v = body_json(resp).await;
        assert_eq!(v["error"], "napcat_not_configured");
    }

    #[tokio::test]
    async fn qrcode_returns_503_when_napcat_unreachable() {
        let tmp = TempDir::new().unwrap();
        // Bind a listener and accept-then-close every connection without
        // ever speaking HTTP. reqwest sees an abrupt EOF mid-response
        // and surfaces a transport error, which we map to 503
        // `napcat_unavailable`. This is faster + more deterministic than
        // picking an "unused" port (macOS aggressively reuses ports and
        // some devboxes forward port 1).
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else {
                    break;
                };
                drop(socket);
            }
        });
        let state = state_with_napcat(Some(format!("http://{addr}")), tmp.path().to_path_buf());
        let app = app_with(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/qrcode")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = resp.status();
        let v = body_json(resp).await;
        assert_eq!(
            v["error"], "napcat_unavailable",
            "got status={status} body={v:?}"
        );
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn happy_path_qrcode_then_confirmed_writes_accounts_file() {
        let tmp = TempDir::new().unwrap();
        let qrcode_body = json!({
            "code": 0,
            "data": { "qrcode": "iVBORw0KGgoAAAA_fake_base64" }
        });
        let status = Arc::new(tokio::sync::Mutex::new(json!({
            "code": 0,
            "data": { "isLogin": false, "qrcodeurl": "https://ptqrshow.example/x" }
        })));
        let url =
            spawn_mock_napcat(qrcode_body, status.clone(), json!({"code": 0, "data": {}})).await;

        let state = state_with_napcat(Some(url), tmp.path().to_path_buf());
        let app = app_with(state.clone());

        // 1) qrcode
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/qrcode")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        let token = v["token"].as_str().unwrap().to_string();
        assert_eq!(v["image_base64"], "iVBORw0KGgoAAAA_fake_base64");
        assert!(v["qrcode_url"].is_null());
        assert!(v["expires_at"].as_u64().unwrap() > 0);

        // 2) status: still waiting
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri(format!("/admin/channels/qq/qrcode/status?token={}", token))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["status"], "waiting");

        // Flip the mock to confirmed.
        *status.lock().await = json!({
            "code": 0,
            "data": {
                "isLogin": true,
                "uin": "10001",
                "nick": "Tester",
                "avatarUrl": "https://example.com/a.png"
            }
        });

        // 3) status: confirmed + account
        let resp = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri(format!("/admin/channels/qq/qrcode/status?token={}", token))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["status"], "confirmed");
        assert_eq!(v["account"]["uin"], "10001");
        assert_eq!(v["account"]["nickname"], "Tester");

        // 4) accounts file written
        let path = tmp.path().join(ACCOUNTS_FILE);
        assert!(path.exists(), "accounts file should exist after confirm");
        let list = load_accounts(&path).await.unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].uin, "10001");

        // 5) /accounts endpoint returns the same.
        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/channels/qq/accounts")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["accounts"][0]["uin"], "10001");
    }

    #[tokio::test]
    async fn expired_qrcode_reports_expired() {
        let tmp = TempDir::new().unwrap();
        let status = Arc::new(tokio::sync::Mutex::new(json!({
            "code": 0,
            "data": { "isLogin": false, "qrcodeurl": "" }
        })));
        let url = spawn_mock_napcat(
            json!({"code": 0, "data": {"qrcode": "x"}}),
            status,
            json!({"code": 0, "data": {}}),
        )
        .await;
        let state = state_with_napcat(Some(url), tmp.path().to_path_buf());
        let app = app_with(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .uri("/admin/channels/qq/qrcode/status?token=abc")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["status"], "expired");
    }

    #[tokio::test]
    async fn quick_login_upserts_account() {
        let tmp = TempDir::new().unwrap();
        // Seed an existing account first.
        let path = tmp.path().join(ACCOUNTS_FILE);
        upsert_account(
            &path,
            &QqAccount {
                uin: "99999".into(),
                nickname: Some("old".into()),
                avatar_url: None,
                last_login_at: 1,
            },
        )
        .await
        .unwrap();

        let status = Arc::new(tokio::sync::Mutex::new(json!({"code": 0, "data": {}})));
        let quick = json!({
            "code": 0,
            "data": { "isLogin": true, "uin": "99999", "nick": "fresh" }
        });
        let url =
            spawn_mock_napcat(json!({"code": 0, "data": {"qrcode": "x"}}), status, quick).await;
        let state = state_with_napcat(Some(url), tmp.path().to_path_buf());
        let app = app_with(state);

        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/quick-login")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"uin":"99999"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["status"], "confirmed");
        assert_eq!(v["account"]["uin"], "99999");
        assert_eq!(v["account"]["nickname"], "fresh");

        let list = load_accounts(&path).await.unwrap();
        assert_eq!(list.len(), 1);
        // Nickname should have been updated, last_login_at refreshed.
        assert_eq!(list[0].nickname.as_deref(), Some("fresh"));
        assert!(list[0].last_login_at > 1);
    }

    #[tokio::test]
    async fn quick_login_rejects_empty_uin() {
        let tmp = TempDir::new().unwrap();
        let state = state_with_napcat(Some("http://127.0.0.1:1".into()), tmp.path().to_path_buf());
        let app = app_with(state);
        let resp = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/admin/channels/qq/quick-login")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"uin":""}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[test]
    fn classify_qrcode_detects_url_vs_base64() {
        let (b64, url) = classify_qrcode("https://ptqrshow.example/123");
        assert!(b64.is_none() && url.is_some());
        let (b64, url) = classify_qrcode("iVBORw0KGgo...");
        assert!(b64.is_some() && url.is_none());
        let (b64, _url) = classify_qrcode("data:image/png;base64,iVBORw0KGgo");
        assert_eq!(b64.as_deref(), Some("iVBORw0KGgo"));
    }
}
