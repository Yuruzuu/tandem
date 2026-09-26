# Contributing

Thanks for helping out. Bug reports, fixes and documentation improvements are all welcome.

## Reporting bugs

Open an issue with:

- your Windows, Codex desktop, Codex CLI and Claude Code versions (`codex --version`, `claude --version`)
- what you did, what you expected, and what happened
- the output of `plugins\tandem\scripts\tandem.cmd status`

Never paste the contents of `%USERPROFILE%\.codex\tandem`, your `config.toml`'s `openai_base_url`, or any credentials. They contain secrets.

## Making changes

1. Fork and create a branch.
2. Keep changes focused, and match the style of the surrounding code.
3. Add or update tests in `plugins/tandem/tests`. They must run offline; use `tests/fake_claude.py` instead of a real Claude Code process.
4. Run the suites:

   ```powershell
   cd plugins\tandem
   python -B -m unittest discover -s tests -p "test_*.py" -v
   $env:PYTHON = "python"; node --test tests/router.test.cjs
   ```

5. Open a pull request describing what changed and how you tested it. If you checked the change live in Codex, say so.

## Design rules

- GPT traffic must stay unchanged and must only ever go to the fixed OpenAI endpoint.
- OpenAI credentials must never reach the bridge or Claude Code, and Tandem must never read credential files.
- Claude Code's own shell and edit tools stay disabled; commands and edits go through Codex.
- Configuration changes must stay reversible and must not touch unrelated settings.
