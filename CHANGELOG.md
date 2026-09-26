# Changelog

## 0.1.0 — 2026-09-26

First public release.

- Adds **Claude Opus 5.5 (Claude Code)** to the Codex desktop model picker alongside GPT.
- Claude Code runs the agent loop; Codex runs relayed `exec_command`, `write_stdin` and `apply_patch` calls and shows native command and diff cards.
- Summarized thinking and Claude Code activity appear in Codex's reasoning view.
- One resumable Claude Code session per Codex chat, with history checkpoints so work done with GPT carries over when switching back.
- Reversible setup through the plugin's `enable` and `restore` tools, with the model catalog built from Codex's own model cache.
