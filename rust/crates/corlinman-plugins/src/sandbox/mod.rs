//! Sandbox strategies for plugin execution.
//
// TODO: trait `Sandbox { async fn wrap(input: PluginInput) -> Result<PluginInput> }`
//       so a runtime can treat sandboxing as a transparent middleware.
// TODO: default impl `NoopSandbox` + cargo-feature-gated `DockerSandbox`.

pub mod docker;
