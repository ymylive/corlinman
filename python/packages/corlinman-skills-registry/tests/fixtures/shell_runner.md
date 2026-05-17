---
name: shell_runner
description: Runs shell commands in a sandboxed environment
metadata:
  openclaw:
    emoji: "🐚"
    requires:
      bins: ["bash"]
      anyBins: ["rg", "grep"]
      config: []
      env: []
allowed-tools:
  - shell.exec
---
# Shell Runner

Executes shell commands and returns stdout/stderr.
