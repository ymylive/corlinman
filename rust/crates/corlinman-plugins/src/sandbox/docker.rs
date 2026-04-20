//! Docker sandbox via bollard — assembles HostConfig from manifest.sandbox fields.
//
// TODO: map `manifest.sandbox` {memory, cpus, readOnlyRoot, capDrop, network, binds}
//       onto `bollard::container::HostConfig`; default image = `docker/Dockerfile.sandbox`.
// TODO: use `AttachContainer` with stdin/stdout streams so stdio runtime works unchanged
//       across bare-process and containerised execution.
