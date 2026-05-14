//! Stdio spawn + reap primitive for the MCP runtime (Phase 4 W3 C2).
//!
//! This is the lowest layer of the C2 stack: a thin wrapper around
//! [`tokio::process::Command`] that gives the upper-layer [`McpAdapter`]
//! a child process whose lifetime is bounded by an owning Rust value.
//! No protocol semantics live here — this module is deliberately ignorant
//! of MCP wire format, JSON-RPC framing, and the [`super::PluginRuntime`]
//! trait. Those land in iters 4-6.
//!
//! Why a dedicated spawn helper instead of reaching for
//! `runtime/jsonrpc_stdio.rs`'s `Command::new(...)` line directly:
//! the existing sync runtime is *spawn-per-call*; the MCP runtime needs
//! a *long-lived* child whose stdin/stdout stay open for the lifetime
//! of the plugin entry, multiplexing many JSON-RPC calls over the
//! same pipes. The lifecycle, drop semantics, and reap behaviour are
//! materially different, so we factor them out.
//!
//! Design contract (from `phase4-w3-c2-design.md` §Lifecycle):
//!   - `kill_on_drop(true)` is *enforced* — a panic in the gateway
//!     must not orphan child processes.
//!   - `cwd` is always the manifest directory; the runtime never
//!     shell-expands or `$HOME`-substitutes.
//!   - `env` starts from a *blank* environment (plus the four
//!     `PATH/HOME/USER/LANG` keys required for `npx`/`uvx` to even
//!     start). The full `env_passthrough` filter and log redaction
//!     land in iter 3.
//!   - Stdin/stdout/stderr are all piped — stderr is captured so the
//!     adapter (iter 4) can fold MCP child noise into gateway tracing.

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use thiserror::Error;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};

/// Names of the env vars that must always pass through to the child,
/// regardless of `env_passthrough.allow`. `npx`/`uvx`/`python` will
/// fail to even reach the MCP server entry point without these.
///
/// The list is intentionally minimal — `LANG` is included because
/// node's intl/icu paths read it and a missing value warps default
/// locale-aware string comparisons in some MCP servers
/// (e.g. filesystem listing order).
pub const REQUIRED_ENV_KEYS: &[&str] = &["PATH", "HOME", "USER", "LANG"];

/// Error returned by [`spawn_mcp_child`].
#[derive(Debug, Error)]
pub enum SpawnError {
    /// The configured `cwd` does not exist or is not a directory.
    #[error("manifest cwd {path} is not a directory")]
    BadCwd { path: PathBuf },

    /// `tokio::process::Command::spawn` returned an OS-level error
    /// (binary not found, ENOENT, permission denied, …).
    #[error("failed to spawn {command}: {source}")]
    Spawn {
        command: String,
        #[source]
        source: std::io::Error,
    },

    /// The spawned child reported a missing pipe — should not happen
    /// because we explicitly request `Stdio::piped()` for all three
    /// fds, but defensive against future tokio surprises.
    #[error("child {pid:?} reported missing stdio pipe ({which})")]
    MissingPipe {
        pid: Option<u32>,
        which: &'static str,
    },
}

/// Owned handle to a spawned MCP child. Drop kills the process.
///
/// All three pipes (`stdin`, `stdout`, `stderr`) are extracted onto the
/// struct so callers can split-borrow them as needed. The [`Child`]
/// remains under the cell so `wait`/`try_wait`/`kill` go through the
/// same handle that owns the pid; this keeps `kill_on_drop` honest.
pub struct McpChild {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: Option<ChildStdout>,
    stderr: Option<ChildStderr>,
}

impl std::fmt::Debug for McpChild {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("McpChild")
            .field("pid", &self.child.id())
            .field("stdin_open", &self.stdin.is_some())
            .field("stdout_open", &self.stdout.is_some())
            .field("stderr_open", &self.stderr.is_some())
            .finish()
    }
}

impl McpChild {
    /// OS pid; `None` once the child has been reaped.
    pub fn pid(&self) -> Option<u32> {
        self.child.id()
    }

    /// Take ownership of the child stdin pipe. After the first call
    /// subsequent calls return `None`.
    pub fn take_stdin(&mut self) -> Option<impl AsyncWrite + Unpin + Send + 'static> {
        self.stdin.take()
    }

    /// Take ownership of the child stdout pipe.
    pub fn take_stdout(&mut self) -> Option<impl AsyncRead + Unpin + Send + 'static> {
        self.stdout.take()
    }

    /// Take ownership of the child stderr pipe.
    pub fn take_stderr(&mut self) -> Option<impl AsyncRead + Unpin + Send + 'static> {
        self.stderr.take()
    }

    /// Wait for the child to exit. Consumes `self` (you can't write
    /// to a process you're awaiting).
    pub async fn wait(mut self) -> std::io::Result<std::process::ExitStatus> {
        self.child.wait().await
    }

    /// Non-blocking exit check.
    pub fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        self.child.try_wait()
    }

    /// Force-kill via SIGKILL on Unix / TerminateProcess on Windows.
    /// Idempotent: a no-op once the child has already exited.
    pub async fn kill(&mut self) -> std::io::Result<()> {
        self.child.kill().await
    }
}

/// Build the canonical environment for an MCP child.
///
/// Strategy:
///   1. Start from a *blank* env.
///   2. Copy [`REQUIRED_ENV_KEYS`] from the parent verbatim (these are
///      what `npx`/`uvx` need to even resolve their entry binary).
///   3. Append the caller-supplied `extra` pairs verbatim. The full
///      allow/deny + redaction story lives in iter 3
///      (`runtime/mcp/redact.rs`); this fn is the dumb plumbing layer.
///
/// Determinism: the iteration order is "required keys first, then
/// extras in supplied order". A duplicate in `extra` overrides an
/// earlier value (incl. a required key) — that matches what
/// `Command::env(K, V)` does on its own.
pub fn build_child_env<I, K, V>(extra: I) -> Vec<(OsString, OsString)>
where
    I: IntoIterator<Item = (K, V)>,
    K: Into<OsString>,
    V: Into<OsString>,
{
    let mut out: Vec<(OsString, OsString)> = Vec::new();
    for k in REQUIRED_ENV_KEYS {
        if let Some(v) = std::env::var_os(k) {
            out.push((OsString::from(*k), v));
        }
    }
    for (k, v) in extra {
        out.push((k.into(), v.into()));
    }
    out
}

/// Spawn an MCP child with all three stdio fds piped.
///
/// Caller responsibilities (deliberately *not* enforced here):
///   - The `command` is on `PATH` or absolute. We surface
///     [`SpawnError::Spawn`] if not.
///   - The `cwd` is a directory the gateway user can `chdir` into;
///     we eagerly check existence before `Command::spawn` so the
///     error is "your cwd is wrong" instead of the often-misleading
///     "ENOENT: command not found" the OS would otherwise report.
///   - `env` is already filtered through allow/deny + redaction
///     — see iter 3.
pub fn spawn_mcp_child(
    command: &str,
    args: &[String],
    cwd: &Path,
    env: Vec<(OsString, OsString)>,
) -> Result<McpChild, SpawnError> {
    if !cwd.is_dir() {
        return Err(SpawnError::BadCwd {
            path: cwd.to_path_buf(),
        });
    }

    let mut cmd = Command::new(command);
    cmd.args(args)
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .env_clear();
    for (k, v) in env {
        cmd.env(k, v);
    }

    let mut child = cmd.spawn().map_err(|e| SpawnError::Spawn {
        command: command.to_string(),
        source: e,
    })?;

    let pid = child.id();
    let stdin = child.stdin.take();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    if stdin.is_none() {
        return Err(SpawnError::MissingPipe {
            pid,
            which: "stdin",
        });
    }
    if stdout.is_none() {
        return Err(SpawnError::MissingPipe {
            pid,
            which: "stdout",
        });
    }
    if stderr.is_none() {
        return Err(SpawnError::MissingPipe {
            pid,
            which: "stderr",
        });
    }

    Ok(McpChild {
        child,
        stdin,
        stdout,
        stderr,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Spawn `cat` with `cwd = tmpdir`, write a payload to stdin,
    /// close stdin, read stdout to EOF — assert round-trip.
    /// This is the smallest possible end-to-end exercise of the
    /// pipe wiring without any MCP / JSON-RPC semantics.
    #[tokio::test]
    async fn cat_round_trip() {
        // `cat` is a POSIX builtin; gate the test on its presence so
        // CI on a Windows runner skips cleanly (we don't ship Windows
        // support today, but the suite shouldn't panic if someone
        // tries).
        if which::which("cat").is_err() {
            eprintln!("cat not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let env = build_child_env(std::iter::empty::<(String, String)>());
        let mut child = spawn_mcp_child("cat", &[], tmp.path(), env).expect("spawn cat");

        let mut stdin = child.take_stdin().expect("stdin pipe");
        let mut stdout = child.take_stdout().expect("stdout pipe");

        // Drop the stderr handle eagerly — `cat` writes nothing there
        // for our happy path but leaving the pipe full would deadlock
        // on a verbose child.
        let _stderr = child.take_stderr().expect("stderr pipe");

        stdin.write_all(b"hello mcp\n").await.unwrap();
        stdin.shutdown().await.unwrap();
        drop(stdin);

        let mut buf = Vec::new();
        stdout.read_to_end(&mut buf).await.unwrap();
        assert_eq!(&buf, b"hello mcp\n");

        // Reap the child to assert clean exit.
        let status = child.wait().await.unwrap();
        assert!(status.success(), "cat exit status: {status:?}");
    }

    /// Spawn a binary that doesn't exist — must surface
    /// [`SpawnError::Spawn`] not panic / hang.
    #[tokio::test]
    async fn missing_binary_returns_error() {
        let tmp = tempfile::tempdir().unwrap();
        let env = build_child_env(std::iter::empty::<(String, String)>());
        let err = spawn_mcp_child(
            "/definitely/not/a/real/binary/c2-iter2",
            &[],
            tmp.path(),
            env,
        )
        .expect_err("missing binary must error");
        assert!(
            matches!(err, SpawnError::Spawn { .. }),
            "expected Spawn variant, got {err:?}"
        );
    }

    /// `cwd` not a directory — must short-circuit before `Command::spawn`
    /// so the error is the more useful [`SpawnError::BadCwd`].
    #[tokio::test]
    async fn bad_cwd_returns_error() {
        let env = build_child_env(std::iter::empty::<(String, String)>());
        let err = spawn_mcp_child("cat", &[], Path::new("/definitely/not/a/dir/c2-iter2"), env)
            .expect_err("bad cwd must error");
        assert!(
            matches!(err, SpawnError::BadCwd { .. }),
            "expected BadCwd variant, got {err:?}"
        );
    }

    /// `kill_on_drop` is enforced — drop the [`McpChild`] without
    /// awaiting it and the child must terminate. We assert this by
    /// spawning a long-running `sleep`, capturing the pid, dropping,
    /// and probing `kill -0 pid` (Unix only) until it returns ESRCH.
    #[cfg(unix)]
    #[tokio::test]
    async fn drop_kills_child() {
        if which::which("sleep").is_err() {
            eprintln!("sleep not on PATH; skipping");
            return;
        }
        let tmp = tempfile::tempdir().unwrap();
        let env = build_child_env(std::iter::empty::<(String, String)>());
        let child =
            spawn_mcp_child("sleep", &["120".to_string()], tmp.path(), env).expect("spawn sleep");
        let pid = child.pid().expect("pid available");
        drop(child);

        // Give the kernel a moment to deliver SIGKILL + reap the zombie.
        // Poll up to ~2s; in practice this is sub-millisecond on Linux.
        let mut alive = true;
        for _ in 0..200 {
            if !pid_alive(pid) {
                alive = false;
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        assert!(!alive, "child pid {pid} survived drop");
    }

    /// Probe whether a pid is alive without reaping it.
    /// On unix `kill(pid, 0)` returns 0 if the process exists, ESRCH
    /// if it doesn't, EPERM if it does but we lack permission.
    /// We treat EPERM as "alive" (defensive).
    #[cfg(unix)]
    fn pid_alive(pid: u32) -> bool {
        // SAFETY: kill(pid, 0) is signal-safe and only checks the
        // process table; it does not deliver a signal.
        let r = unsafe { libc::kill(pid as i32, 0) };
        if r == 0 {
            return true;
        }
        let errno = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
        // ESRCH == 3 on Linux/macOS; EPERM == 1.
        errno == libc::EPERM
    }

    /// `build_child_env` always includes the four required keys when
    /// the parent has them set, plus whatever the caller passes.
    /// Determinism check: caller-supplied dup overrides the inherited
    /// value (matches `Command::env(k, v)` last-wins semantics).
    #[test]
    fn build_child_env_includes_required_keys() {
        // PATH is set in every reasonable test environment.
        let env = build_child_env(std::iter::empty::<(String, String)>());
        let keys: Vec<&str> = env.iter().map(|(k, _)| k.to_str().unwrap_or("")).collect();
        assert!(
            keys.contains(&"PATH"),
            "PATH must be forwarded; got {:?}",
            keys
        );

        let env = build_child_env([("MY_TOKEN".to_string(), "shh".to_string())]);
        let pairs: Vec<(String, String)> = env
            .iter()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().into_owned(),
                    v.to_string_lossy().into_owned(),
                )
            })
            .collect();
        assert!(
            pairs.iter().any(|(k, v)| k == "MY_TOKEN" && v == "shh"),
            "extra pair must be present"
        );
    }

    /// Caller-supplied `PATH` overrides the inherited one (dup wins).
    #[test]
    fn build_child_env_dup_overrides() {
        let env = build_child_env([("PATH".to_string(), "/c2/iter2/path".to_string())]);
        // Take the *last* PATH value; that's what `Command::env` would
        // observe in iteration order.
        let last_path = env
            .iter()
            .rev()
            .find(|(k, _)| k.to_string_lossy() == "PATH")
            .map(|(_, v)| v.to_string_lossy().into_owned());
        assert_eq!(last_path.as_deref(), Some("/c2/iter2/path"));
    }
}
