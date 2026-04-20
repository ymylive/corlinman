//! Build script: compile all `proto/corlinman/v1/*.proto` files via tonic-build.
//!
//! The proto directory lives two levels above this crate
//! (`rust/crates/corlinman-proto/` → workspace root → `proto/`).
//!
//! Rebuilds are triggered when any proto file changes.

use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto_root = PathBuf::from("../../../proto");
    let v1_dir = proto_root.join("corlinman/v1");

    let protos = [
        v1_dir.join("common.proto"),
        v1_dir.join("llm.proto"),
        v1_dir.join("embedding.proto"),
        v1_dir.join("vector.proto"),
        v1_dir.join("plugin.proto"),
        v1_dir.join("agent.proto"),
    ];

    for p in &protos {
        println!("cargo:rerun-if-changed={}", p.display());
    }
    println!("cargo:rerun-if-changed=build.rs");

    tonic_build::configure()
        .build_client(true)
        .build_server(true)
        .compile_protos(&protos, &[proto_root])?;

    Ok(())
}
