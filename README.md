# Tandem

**Use Claude Code as a model in the Codex desktop app.**

Tandem is a Codex plugin that adds **Claude Opus 5.5 (Claude Code)** to the Codex model picker, next to your GPT models. When you pick it, your own Claude Code installation runs the agent loop: its system prompt, reading and search tools, web tools, skills, subagents, MCP servers, `CLAUDE.md` and context management. Shell commands and file edits are handed back to Codex, which runs them with its normal approvals and sandbox and shows them as its own command and diff cards.

GPT keeps working exactly as before: same default model, same login, same requests.

> [!WARNING]
> Tandem is an independent, experimental project. It is not affiliated with, endorsed by, or supported by Anthropic or OpenAI. It uses **your** Claude Code installation and **your** Claude login or API key, so your agreement with Anthropic governs that usage. Read [Terms and account risk](#terms-and-account-risk) before using it with a Claude subscription.

> [!TIP]
> **Why Tandem runs Claude Code as the harness instead of plugging Opus into Codex directly**
>
> The obvious way to put Opus in Codex is to treat it as a bare model: Codex sends its own prompt and tools, and something in between turns Claude Code into a raw model endpoint. Tandem's first version worked that way. It was dropped because:
>
> - **Your Claude login sat in the middle.** Claude Code's requests to Anthropic went through a local relay that forwarded your login token and edited the request body. Tandem doesn't touch Claude Code's traffic: the unmodified CLI talks straight to Anthropic, and nothing in Tandem ever sees your token.
> - **It used Claude Code as a model API, not as Claude Code.** Tandem runs Claude Code with its own system prompt, tools, skills and context management, which is much closer to ordinary Claude Code use. It is still a third-party front end, so the warning above still applies.
> - **It cost far more.** Codex sends every tool definition it has with each request: about 225k input tokens per request in testing, with no cache hits. Tandem requests were about 25k and mostly served from Anthropic's prompt cache.
>
> What didn't change: commands and file edits still run through Codex with its approvals and sandbox (Claude Code's own shell and edit tools are disabled), and GPT requests and OpenAI credentials never reach Claude Code.

## What you get

- **Claude Code's harness inside Codex.** One long-lived `claude -p` session per Codex chat, resumed automatically after restarts.
- **Native Codex cards.** `exec_command`, `write_stdin` and `apply_patch` are relayed from Claude Code to Codex. Codex executes them, asks for approval if your settings require it, and renders the usual command output and diffs.
- **Visible thinking.** Claude's summarized thinking streams into Codex's reasoning view, and other Claude Code activity appears as short lines such as "Read `src/app.ts`" or "Started subagent".
- **Model switching.** Work done with GPT in the same chat is forwarded when you switch back to Claude Code, and your `AGENTS.md` and environment context are passed along.
- **Reversible setup.** Enabling changes exactly two settings in `config.toml` and saves a backup; restoring puts them back without touching anything else.

## How it works

```
Codex desktop ──► local router (127.0.0.1:57856)
                    ├─ GPT models ─────────────► chatgpt.com (unchanged request, your OpenAI login)
                    └─ Claude Opus 5.5 (Claude Code)
                         └─► local bridge (127.0.0.1:57855)
                               └─► claude -p  (your Claude Code, your Claude login)
                                     └─ codex MCP relay: exec_command / apply_patch
                                          └─► returned to Codex as native tool calls ──► Codex runs them and shows cards
```

1. Enabling Tandem points Codex's `openai_base_url` at a local router and adds the Claude Code model to the catalog Codex reads (`model_catalog_json`). The catalog is built from Codex's own cached model list, so your GPT models stay as they are.
2. The router forwards GPT traffic untouched to OpenAI. Requests for the Claude Code model go to the local bridge instead; OpenAI credentials are never sent there.
3. The bridge keeps one Claude Code process per Codex chat. Claude Code's own Bash, PowerShell, Edit and Write tools are disabled; instead it gets a `codex` MCP server whose tools mirror Codex's shell and patch tools.
4. When Claude calls one of those tools, the bridge ends the current Codex response with a real Codex tool call. Codex executes it, then sends the result back, and the bridge hands that result to the Claude Code process that is waiting for it.

## Requirements

| | |
|---|---|
| OS | Windows 10/11. The code has POSIX paths, but only Windows is tested and the plugin launcher is a `.cmd` file. |
| Codex | Codex desktop app, signed in with ChatGPT. Tested with desktop 26.924 and Codex CLI 0.157. The Codex CLI is used for installation. |
| Claude Code | Installed and signed in (`claude auth login`), with access to Claude Opus 5.5. Tested with Claude Code 2.1.283. |
| Python | 3.11+ with `cryptography`. Codex desktop's bundled runtime is used automatically; otherwise set `TANDEM_PYTHON`. |
| Node.js | 22.15+ (for zstd support). Codex desktop's bundled runtime is used automatically; otherwise `node` on `PATH` or `TANDEM_NODE`. |

## Install

1. Add the marketplace and install the plugin:

   ```powershell
   codex plugin marketplace add Yuruzuu/tandem
   codex plugin add tandem@tandem
   ```

2. Open Codex and ask: **"Enable Tandem"**. Codex calls the plugin's `enable` tool (approve it when asked).

   Or run it yourself from a clone of this repository:

   ```powershell
   git clone https://github.com/Yuruzuu/tandem
   .\tandem\plugins\tandem\scripts\tandem.cmd enable-desktop
   ```

3. Fully quit and restart Codex. Pick **Claude Opus 5.5 (Claude Code)** in the model picker. Your GPT default stays selected for new chats.

Enabling refuses to run if you already use a custom `model_provider` or `openai_base_url`, so it never overwrites someone else's routing.

## Usage notes

- **Reasoning effort** in Codex maps to Claude Code's `--effort` (low, medium, high, xhigh, max).
- **Approvals.** Relayed commands and edits follow your Codex approval and sandbox settings. Claude Code's *other* tools (reads, web access, its own MCP servers) run with `bypassPermissions`, outside Codex's approvals.
- **What is not shared.** Codex's own developer instructions, skills, plugins and MCP servers are not passed to Claude Code. Claude Code uses its own configuration instead (`~/.claude`, `CLAUDE.md`, its MCP servers).
- **Interrupting** a turn stops that chat's Claude Code process; your next message resumes the saved session.
- **Compaction.** Claude Code compacts its own context. If Codex compacts a Claude Code chat, Codex keeps only a pointer to the Claude Code session, so switching that chat to GPT afterwards loses the earlier details. Opaque GPT compaction likewise can't be imported into Claude Code. Start a fresh chat for those switches.
- **Idle sessions** are stopped after an hour and resumed on demand.
- **Tokens.** Each request carries Claude Code's own system prompt and tool definitions (about 25k tokens in testing, almost all served from Anthropic's prompt cache after the first request). MCP tools are loaded on demand by Claude Code.

## Uninstall

```powershell
# Ask Codex "Restore the direct OpenAI route" (plugin tool `restore`), or:
.\tandem\plugins\tandem\scripts\tandem.cmd restore-desktop
codex plugin remove tandem@tandem
codex plugin marketplace remove tandem
```

Restart Codex afterwards. You can then delete `%USERPROFILE%\.codex\tandem`. Keep it if you might reinstall: it holds the key needed to read Claude Code turns in your existing chats.

## Troubleshooting

**Codex can't reach any model (GPT included).** All model traffic goes through the local router while Tandem is enabled. The plugin starts it when Codex loads, but if it isn't running:

```powershell
.\tandem\plugins\tandem\scripts\tandem.cmd start     # start bridge and router
.\tandem\plugins\tandem\scripts\tandem.cmd status    # check both
.\tandem\plugins\tandem\scripts\tandem.cmd restore-desktop   # or switch back to the direct OpenAI route
```

`restore-desktop` works without the router or Codex running.

**"Codex has not cached its model list yet."** Open Codex once (so it writes `models_cache.json`), then enable again.

**The Claude Code model errors immediately.** Check `claude --version` and that `claude` works in a terminal and is signed in. Set `TANDEM_CLAUDE` to the full path of the `claude` executable if Codex can't find it.

**New GPT models are missing.** The combined catalog is rebuilt from Codex's model cache whenever the plugin starts. Restart Codex to pick up changes.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TANDEM_HOME` | `$CODEX_HOME\tandem` | Private state: route secret, history key, session map, config backups |
| `TANDEM_PYTHON` | Codex's bundled Python, then `python` | Python used to run the plugin |
| `TANDEM_NODE` | Codex's bundled Node.js, then `node` | Node.js used to run the router |
| `TANDEM_CLAUDE` | `claude` on `PATH` | Claude Code executable |

The bridge and router listen on `127.0.0.1:57855` and `127.0.0.1:57856`.

## Security and privacy

- Everything runs locally on loopback. The router URL contains a random secret, the bridge requires it as a bearer token, and each Claude Code session's relay uses its own token. Requests with a browser `Origin` header are rejected.
- GPT requests are forwarded unchanged to the fixed `https://chatgpt.com/backend-api/codex` endpoint; the router has no setting that could redirect them. The only exception: when a chat switches from Claude Code to GPT, Tandem's own encrypted items are removed from the history first, because OpenAI can't read them.
- Tandem never reads your OpenAI or Claude credential files. Claude Code handles its own login.
- `$CODEX_HOME\tandem` contains the route secret, the key that encrypts Claude Code checkpoints stored in your Codex history, and backups of your `config.toml`. Don't share it.

Please report security issues privately; see [SECURITY.md](SECURITY.md).

## Terms and account risk

Tandem runs the official, unmodified Claude Code CLI with your own login or API key, and all usage is billed to your own account. It does not intercept, store or forward your Claude credentials.

Anthropic's [Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance) describes subscription (Free, Pro, Max) sign-in as intended for ordinary use of Claude Code and Anthropic's own applications, and asks developers building products on Claude to use API keys. Using a subscription to power Claude Code inside another app's interface may fall outside what Anthropic permits, and Anthropic says it may enforce its restrictions without notice. You are responsible for deciding whether your use complies. If you want certainty, sign Claude Code in with an [Anthropic API key](https://platform.claude.com/) instead of a subscription.

Tandem also changes where the Codex app sends its model requests (to a local router that forwards GPT traffic unchanged). OpenAI's terms continue to apply to that traffic.

## Development

```powershell
cd plugins\tandem
python -B -m unittest discover -s tests -p "test_*.py" -v
$env:PYTHON = "python"; node --test tests/router.test.cjs
```

The tests run fully offline. A fake `claude` process (`tests/fake_claude.py`) stands in for Claude Code, so no model calls are made.

Layout:

```
.agents/plugins/marketplace.json   Codex marketplace manifest
plugins/tandem/
  .codex-plugin/plugin.json        plugin manifest
  .mcp.json                        management MCP server (status / enable / restore)
  models.json                      the Claude Code model's catalog entry
  scripts/control.py               setup, restore and process management
  scripts/router.cjs               local router in front of all Codex model traffic
  scripts/bridge.py                local Responses endpoint for the Claude Code model
  scripts/harness.py               Claude Code sessions and the Codex tool relay
  scripts/native/relay_mcp.py      MCP server inside each Claude Code session
  skills/tandem/SKILL.md           instructions Codex uses to manage Tandem
```

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

Process and schema helpers in `scripts/native/cli.py` are adapted from Nous Research's [hermes-plugin-claude-subscription-directsdk](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk) under the MIT license (see [`plugins/tandem/scripts/native/LICENSE`](plugins/tandem/scripts/native/LICENSE)).

## License

[MIT](LICENSE)
