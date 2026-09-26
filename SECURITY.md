# Security policy

Tandem sits in front of all of Codex's model traffic, so security reports are taken seriously.

Please **do not** open a public issue for vulnerabilities. Report them privately through [GitHub security advisories](https://github.com/Yuruzuu/tandem/security/advisories/new).

Include what an attacker could do, the conditions required (for example, another local user or a malicious web page), and steps to reproduce. You'll get an acknowledgement as soon as possible.

Things that are in scope include: ways to reach the local router, bridge or relay without the secret; GPT requests or OpenAI credentials being sent anywhere other than the official endpoint; Claude Code gaining shell or file-write access outside the Codex relay; and configuration changes that are not reversible.
