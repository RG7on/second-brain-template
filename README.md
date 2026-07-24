# brain

A permanent second brain for your AI: plain markdown in git, wired so that any
Claude session on your machine can search it, read it, and add to it — and
built to stay accurate as it gets large, not just to get large.

## What this is

Your knowledge, as files you own: decisions and the reasoning behind them,
reference notes, projects, people, and life context. Plus the wiring that lets
Claude reach it — an MCP server and a `/brain` skill that live in this repo and
get installed once, so every session in every directory has the same access
without being told about it each time.

The bet is that the hard part of a second brain is not *storing* things. It is
still trusting what it tells you after a thousand notes. So most of this system
is machinery for keeping knowledge true: a schema enforced at commit time, a
supersede protocol that moves outdated notes physically out of search, a link
graph that catches references rotting, and a weekly pass that curates the pile.

## What it does today

Everything here is implemented and covered by tests.

- **Capture and retrieve from any session.** `brain_search`, `brain_read`,
  `brain_links`, `brain_recent`, `brain_capture` over MCP; the same operations
  as a CLI. Notes are found by wording (SQLite FTS5, BM25 over
  title/aliases/topics/body, folder-weighted) or by relationship (the
  `[[wikilink]]` backlink graph).
- **Stays fast as it grows.** At 10,000 notes on a laptop: read a note ~100 ms,
  search ~120 ms, a full lint of the corpus ~1.9 s. Every index is derived from
  the notes and rebuilt, never authoritative.
- **Refuses to serve stale knowledge.** Superseded notes get `status:
  superseded` and move to `archive/`, which is excluded from search by
  construction rather than by remembering to filter. `brain read` follows a
  supersede chain to the current version and warns when a note is archived,
  provisional, or past its `review_by`.
- **A commit gate that actually blocks.** Schema violations, secrets, unresolved
  merge-conflict markers, dangling `[[links]]`, duplicate ids, and half-finished
  supersedes are refused at `git commit` — and again in CI.
- **Generous capture, strict promotion.** Quick thoughts land in `inbox/`,
  outside default search. A weekly Claude pass drains them into real notes or
  deletes them, mines the week's sessions for things you'd otherwise lose, and
  lands on a branch for review — never on `main`.
- **Yours, and backed up.** Every commit auto-pushes to your private repo. A
  nightly health check reports hooks, backup freshness, inbox backlog and lint.
  Sensitive notes can be `age`-encrypted into `vault/`.
- **Publishable without leaking.** `bin/brain template <dest>` generates a clean
  copy of the system with none of your notes, refusing to finish if a note, key
  or secret would ship.

## What it deliberately does not do

Listed because a second brain that oversells itself is exactly the kind you
stop trusting.

- **No semantic search.** Retrieval is lexical plus structure. If your words
  differ completely from the note's words, it can miss where an
  embeddings-based system would not. Deferred on purpose, not overlooked.
- **No contradiction detection between two current notes.** The system fights
  staleness structurally (supersede, `review_by`, link validation), but nothing
  automatically notices that two live notes disagree. Consolidation and your own
  reading are the backstop.
- **It does not host your other skills, tool connections, or service
  integrations.** Only this brain's own wiring lives here. Treating the repo as
  a general home for all AI configuration is a direction, not a feature.
- **Claude-only tooling.** The knowledge is plain markdown any model can read;
  the toolbelt and MCP server target Claude. See
  [the decision](knowledge/decisions/2026-07-22-claude-only-tool-layer.md).
- **Local only.** No web or mobile access — the MCP server runs on your machine.
- **The privacy rule is instruction-enforced.** "Ask before recording anything
  about a person's private life" is followed by the model, not enforced by code,
  and commits auto-push. Weaker harnesses will eventually get this wrong.

## What it is for

The aim is a base that gets more useful the longer it runs: something that knows
what you decided and why, is already set up the way you work, and is still
correct in ten years. Concretely, that means it has to be:

- **Permanent.** Files in git. Nothing here expires, and nothing is trapped in
  someone else's product.
- **Trustworthy as it grows.** Being large and being accurate are both required.
  Value must not drop just because the pile got big.
- **Instantly retrievable.** Knowing everything only matters if the right thing
  surfaces the moment it is needed.
- **Yours.** The knowledge and its history stay under your control.
- **Model-independent where it counts.** The substrate is markdown and git, so
  switching models loses nothing — a property of the data, not a promise about
  the tooling.

---

*Start here: [SETUP.md](SETUP.md) to install it, [CLAUDE.md](CLAUDE.md) for how
it works and the rules notes are held to.*
