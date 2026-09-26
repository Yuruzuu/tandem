---
name: tandem
description: Enable, disable or check Tandem, which adds Claude Opus 5.5 (Claude Code) to the Codex model picker. Claude Code runs the agent loop; Codex runs its shell commands and file edits.
---

Use the `tandem` MCP server's tools:

- `status` checks the local bridge and router without calling a model.
- `enable` adds **Claude Opus 5.5 (Claude Code)** next to the GPT models. It routes Codex model requests through a local router: GPT requests go unchanged to the normal OpenAI backend with the user's login, and only the Claude Code model goes to the local bridge. It changes only `openai_base_url` and `model_catalog_json` in `config.toml` and saves a backup. Tell the user to restart Codex.
- `restore` puts back the direct OpenAI route and the previous catalog without touching other settings. Tell the user to restart Codex.

If the tools are unavailable, run `scripts/tandem.cmd <status|start|stop|enable-desktop|restore-desktop>` from this plugin's folder (two levels above this file).

How the model works: Claude Code (the user's own installation and Claude login) owns the agent loop, reading and search tools, web tools, skills, subagents, its own MCP servers and context compaction. Codex executes the relayed `exec_command`, `write_stdin` and `apply_patch` calls with its normal approvals and sandbox and shows them as native cards. Claude Code's own shell and edit tools are always disabled. Claude Code's other tools and MCP servers run with `bypassPermissions`, outside Codex's approvals.

Limits to explain when relevant: opaque GPT compaction cannot be imported into a Claude Code session; after Codex compacts a Claude Code chat, GPT only sees a pointer to the Claude Code session, not its full history. Suggest a fresh chat for those switches. Report real errors; do not claim a model is unavailable or silently switch models. Never read credential files. Private state (route secret, history key, session map, config backups) lives in `$CODEX_HOME/tandem`; do not delete or publish it.
