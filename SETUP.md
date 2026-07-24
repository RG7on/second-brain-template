# SETUP — the complete guide, in one file

Everything needed to go from nothing to a fully working second brain wired
into every Claude surface on your machine. Follow top to bottom; each part
ends with a way to verify it worked. The fast path is Parts 1-3 + Part 8
(about 5 minutes); everything else is optional power.

---

## Part 0 — What you are setting up

| Piece | What it gives you |
|---|---|
| The repo (`~/brain`) | Your knowledge as markdown files in git — the only source of truth |
| Git hooks | Every commit is validated (schema + secrets) and auto-pushed to your private backup |
| `bin/brain` | The toolbelt: create/search/supersede notes, lint, health checks |
| `bin/brain-mcp` | A local MCP server — how Claude sessions *anywhere* read and write your brain |
| Claude wiring | MCP registration + `/brain` skill + a global routing rule, so Claude uses the brain without being told |
| Schedules | Nightly health report; optional weekly AI tidy-up |
| Vault | Optional encrypted storage for sensitive notes |

Requirements: macOS with Python 3.9+ and git; [Claude Code](https://claude.com/claude-code)
(CLI or desktop app). Optional but recommended: `brew install gh ripgrep gitleaks age`
(`gh` is the GitHub CLI used in Parts 1 and 8 — run `gh auth login` once after
installing; every gh step also has a no-gh alternative). Linux works too —
skip the Keychain/launchd parts and use your platform's equivalents.

---

## Part 1 — Get the code

**Option A (recommended): GitHub template.** On the template repository
(https://github.com/RG7on/second-brain-template) click **Use this template →
Create a new repository**, name it (e.g. `my-brain`), and — important —
set visibility to **Private** (your knowledge will live here). Then:

```sh
git clone https://github.com/<you>/my-brain.git ~/brain
cd ~/brain
```

**Option B: plain clone, remote later.**

```sh
git clone https://github.com/RG7on/second-brain-template.git ~/brain
cd ~/brain
git remote remove origin      # drop the template remote FIRST — it must not stay 'origin'
gh repo create my-brain --private --source . --push   # creates YOUR private repo as the new origin
```

No `gh`? Create a **private** repository in the GitHub web UI, then:

```sh
git remote add origin https://github.com/<you>/my-brain.git
git push -u origin main
```

Your notes must never live in a public repo. Verify (or just look at the repo
page in your browser — it must say Private):

```sh
gh repo view --json visibility -q .visibility    # must print: PRIVATE
```

---

## Part 2 — One command wires the machine

```sh
bin/brain init
```

This does five things (each idempotent — safe to re-run):

1. Installs the git hooks (`core.hooksPath = .githooks`) — actually, *every*
   `bin/brain` command self-installs these, so hooks can never silently be missing.
2. Writes `.mcp.json` with this clone's absolute path — Claude Code sessions
   *inside* the repo get the brain tools.
3. Renders `setup/skills/brain/SKILL.md` for this clone's path.
4. Symlinks the `/brain` skill into `~/.claude/skills/`.
5. Registers the MCP server at **user scope** (`claude mcp add --scope user
   brain <repo>/bin/brain-mcp`) — Claude Code sessions in *any* directory get
   the brain tools. If the `claude` CLI isn't installed, it prints the exact
   command to run later.

The two files init generates (`.mcp.json` and `setup/skills/brain/SKILL.md`)
contain this machine's absolute paths and are gitignored — every machine
regenerates its own via `bin/brain init`; don't commit them.

**Manual equivalents** (only if you want to do it by hand or `init` failed —
all five steps, in order):

```sh
git config core.hooksPath .githooks
printf '{\n  "mcpServers": {\n    "brain": {"command": "%s/bin/brain-mcp", "args": []}\n  }\n}\n' "$(pwd)" > .mcp.json
sed "s|{{REPO}}|$(pwd)|g" setup/skills/brain/SKILL.md.template > setup/skills/brain/SKILL.md
ln -sfn "$(pwd)/setup/skills/brain" ~/.claude/skills/brain
claude mcp add --scope user brain "$(pwd)/bin/brain-mcp"
```

Verify:

```sh
claude mcp list          # → brain: .../bin/brain-mcp - ✔ Connected
bin/brain doctor         # → [ok ] git hooks installed
```

---

## Part 3 — Tell Claude the brain exists (global routing)

Add this to `~/.claude/CLAUDE.md` (create the file if missing). If you didn't
clone to `~/brain`, replace the path in **both** places it appears below:

```markdown
# brain (second brain)
My permanent knowledge lives at ~/brain (protocol: its CLAUDE.md).
When I ask about my own decisions, projects, people, preferences, or history —
or say "remember this" — use the brain MCP tools (brain_search / brain_read /
brain_links / brain_capture), or work in ~/brain directly per its CLAUDE.md.
Present only current knowledge as true today; archived notes are history.
Trigger: /brain

Offer to capture — do not wait to be asked. When I make a decision and reject
an alternative, state a preference or constraint, or we land an answer that
cost real effort, say so in one line ("worth saving to your brain?") and carry
on. Do not save it for the end of the session: by then the reasoning is gone,
and the reasoning is the whole point. Capture the WHY, not the what.

Never capture a named person's private life, health, finances or relationships
without asking me first — that repo auto-pushes to GitHub. Skip anything
derivable from code or public docs, transient state, and credentials.
Full policy: "What earns a note" in ~/brain/CLAUDE.md.
```

This is what makes Claude *reach for* the brain unprompted. Without it the
tools exist but nothing routes questions to them. **Both halves matter**: the
first paragraph makes Claude RETRIEVE, the second makes it OFFER TO CAPTURE.
Ship only the first and you get a brain that answers but never grows — every
session's reasoning is lost at the moment it was worth keeping.

**Restart your Claude sessions now** — MCP servers and skills load at session
start.

---

## Part 4 — Claude Desktop chat (optional)

Normal desktop chat can use the brain too. Edit
`~/Library/Application Support/Claude/claude_desktop_config.json` and **merge**
this in (don't delete existing keys; create the file if absent):

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/<you>/brain/bin/brain-mcp",
      "args": []
    }
  }
}
```

The path must be absolute. Restart the Claude Desktop app. Chat now has
`brain_search` / `brain_read` / `brain_recent` / `brain_capture` — and capture
still commits + pushes, because the server does the git work itself.

## Part 5 — Cowork (optional)

In Cowork, connect `~/brain` as a folder. Local Cowork sessions can then
read/write your notes directly (guided by the repo's `CLAUDE.md`). Notes:
local sessions only — remote/cloud sessions can't reach local folders or
local MCP servers. Don't worry about Cowork breaking things: the commit gate
validates whatever any agent writes, and the nightly doctor flags anything
left uncommitted.

## Part 6 — claude.ai web and mobile

Not supported — they can't run local processes. (Closable later by hosting
`brain-mcp` behind HTTP as a custom connector; deliberately not part of v1.)

---

## Part 7 — Schedules (recommended)

```sh
bin/brain schedule install --with-consolidate   # nightly doctor + weekly tidy
bin/brain schedule install                      # doctor only, no consolidation
```

The nightly doctor writes `.cache/doctor-report.txt` and shows a macOS
notification only when something is red.

Install the consolidation job too — it is not decoration. The capture policy in
`CLAUDE.md` deliberately captures generously into `inbox/`, and consolidation
is the only thing that turns those into real notes or deletes them. Without it
the inbox grows forever and nothing is ever promoted. It runs Claude headless
weekly, mines the past week's sessions for things you meant to record, and
always lands on a `consolidate/` branch for you to review and merge — never on
main. `bin/brain doctor` flags it when it is missing.

Running it by hand instead is fine: `bin/brain consolidate`. Doing neither is
the one option that quietly breaks the system.

## Part 8 — First run: prove the whole loop

```sh
bin/brain capture "The brain is alive as of $(date +%F)" --commit
bin/brain search "brain alive" --scope all
bin/brain doctor
```

Expected: capture prints the file path and "captured and committed — push
runs in background". Search (the `--scope all` matters — fresh captures live
in the inbox, which default search deliberately excludes until consolidation
promotes them) returns your capture tagged `[provisional — unconsolidated]`.
Doctor shows `[ok ]` on every core line — hooks, remote, pushes, lint;
`[-- ]` lines are informational (optional tools not installed, index not
built until first search), and the inbox line will count the 1 capture
waiting for consolidation. That's a healthy report. Then open a NEW Claude
session anywhere and ask: *"what's in my brain from today?"* — it should call
`brain_recent`/`brain_search` on its own.

---

## Part 9 — Vault for sensitive notes (optional)

For notes too sensitive for plaintext on GitHub (health, money, IDs):

```sh
brew install age
mkdir -p ~/.config/brain
age-keygen -o ~/.config/brain/vault-key.txt
security add-generic-password -a "$USER" -s brain-vault-key \
  -w "$(cat ~/.config/brain/vault-key.txt)" -U
age-keygen -y ~/.config/brain/vault-key.txt > setup/vault-recipient.txt
git add setup/vault-recipient.txt && git commit -m "vault: add public recipient"
```

Also copy the private key into your password manager — **the key IS the
vault**; lose both copies and encrypted notes are gone forever. Encrypting
and reading: see `setup/runbooks/vault.md`. Lint enforces the boundary: no
plaintext in `vault/`, no `sensitivity: private` outside it, everywhere.

## Part 10 — Second machine

```sh
git clone https://github.com/<you>/my-brain.git ~/brain
cd ~/brain && bin/brain init && bin/brain schedule install --with-consolidate
# vault access, if used — write to a temp file FIRST, then move it into place.
# `... -w > key.txt` truncates key.txt to zero bytes before security runs, so
# if the keychain item is missing (the default on a new machine) the redirect
# destroys the very key you were restoring.
mkdir -p ~/.config/brain
security find-generic-password -a "$USER" -s brain-vault-key -w > /tmp/vault-key.$$ \
  && mv /tmp/vault-key.$$ ~/.config/brain/vault-key.txt \
  || { rm -f /tmp/vault-key.$$; echo "no vault key in this machine's Keychain"; }
```

Add the Part 3 block to that machine's `~/.claude/CLAUDE.md`. Done.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Brain tools don't appear in a session | Sessions load MCP at start — open a new session. Check `claude mcp list`; re-run `bin/brain init`. |
| Desktop chat doesn't show the tools | Config path/JSON typo, or app not restarted. Path must be absolute. |
| `commit blocked` with lint errors | That's the system working. Read the errors — each says exactly what to fix. `bin/brain lint` re-checks. |
| `WARNING: the content gate is DOWN` | Lint itself crashed (not your content). Run `python3 bin/brain lint` to see why; commits still work meanwhile. |
| Push rejected: `workflow scope` | Push once from a terminal (`git push`), or `gh auth refresh -s workflow`. |
| Doctor: `no upstream tracking` | `git push -u origin main` once. |
| Doctor: `not pushed — backup is behind; run: git push` | You're offline or the remote rejects; `git push` when back online. |
| `claude: command not found` | Install Claude Code, then `claude mcp add --scope user brain ~/brain/bin/brain-mcp`. |
| Consolidation does nothing on schedule | The `claude` CLI must be logged in for headless runs; run `bin/brain consolidate` manually once to check. |

## Uninstall / undo

```sh
bin/brain schedule uninstall
claude mcp remove --scope user brain
rm ~/.claude/skills/brain                      # removes the symlink only
# remove the brain block from ~/.claude/CLAUDE.md and (if added) from
# claude_desktop_config.json. Your notes remain: they're just files in git.
```
