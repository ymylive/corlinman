//! CLI subcommand modules. Each exposes `pub struct Args` (or `pub enum Cmd`)
//! and `pub async fn run(args) -> anyhow::Result<()>`.

pub mod config;
pub mod dev;
pub mod doctor;
pub mod onboard;
pub mod plugins;
pub mod qa;
