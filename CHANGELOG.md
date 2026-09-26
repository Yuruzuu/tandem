# Changelog

## 0.2.2 — 2026-09-26

- Background subagents work again. When Claude Code pauses to wait for them, Tandem keeps the Codex turn open and streams Claude's follow-up into it once they report back, so Claude can keep working while subagents run. This replaces the 0.2.1 workaround that forced subagents into the foreground.
- A "Subagent completed" line appears in the activity panel when a background subagent finishes.

## 0.2.1 — 2026-09-26

- Fixed turns ending early when Claude Code started a background subagent. Claude Code now runs subagents and other tasks in the foreground, so their results land in the same Codex turn instead of arriving unseen after it ended.

## 0.2.0 — 2026-09-26

- Claude Code's web searches and page fetches now appear in Codex's activity panel as native web search rows.
- Those rows are removed before a GPT request if you switch the chat to GPT.
- New plugin version folder, so updating works while Codex is running.

## 0.1.0 — 2026-09-26

First public release.

- Adds **Claude Opus 5.5 (Claude Code)** to the Codex desktop model picker alongside GPT.
- Claude Code runs the agent loop; Codex runs relayed `exec_command`, `write_stdin` and `apply_patch` calls and shows native command and diff cards.
- Summarized thinking and Claude Code activity appear in Codex's reasoning view.
- One resumable Claude Code session per Codex chat, with history checkpoints so work done with GPT carries over when switching back.
- Reversible setup through the plugin's `enable` and `restore` tools, with the model catalog built from Codex's own model cache.
