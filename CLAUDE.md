# brain — operating manual

This repo is your permanent second brain. The markdown files under
`knowledge/` are the ONLY source of truth; every index or cache is derived and
disposable. Treat everything here as long-lived: notes written today must still
be findable and correct in ten years.

## Layout

```
knowledge/
  index.md        route map — read this first when searching
  topics.yaml     controlled topic vocabulary (flat "topic: alias, alias" lines)
  inbox/          quick captures, relaxed rules, drained by consolidation
  decisions/      one decision per file, YYYY-MM-DD-slug.md, append-only events
  topics/         hub pages that link related notes together
  projects/       <name>/overview.md per project
  people/         one file per person (sensitivity field required)
  life/           open-ended personal areas (sensitivity field required)
  reference/      durable how-tos and facts
  journal/YYYY/   one file per day (YYYY-MM-DD.md), free-form
  vault/          age-ENCRYPTED sensitive notes (.age only, never plaintext)
  archive/        superseded notes, mirror tree — OUT of default search
  attachments/    small binaries, 1MB hard cap per file
setup/            templates, runbooks (this system's own docs)
bin/brain         toolbelt: new | capture | search | read | links | supersede |
                  sessions | consolidate | schedule | lint | doctor
tests/            runtime tests for the toolbelt itself — lint proves the NOTES
                  are well-formed, these prove the TOOLS work:
                  python3 -m unittest discover -s tests
```

## Note contract

Frontmatter is a restricted subset: flat `key: value` pairs and inline
`[a, b]` lists only — no nesting, no multiline. Required on every note in a
canonical folder (decisions/topics/projects/people/life/reference):

```
id:       unique, lowercase-hyphen (decisions: YYYY-MM-DD-slug)
kind:     decision | topic | project | person | note | reference
title:    human-readable
topics:   [list] — every entry must exist in knowledge/topics.yaml
aliases:  [2-5 words future-you might search] — always fill this
created:  YYYY-MM-DD
status:   current   (canonical folders hold current notes ONLY)
```

Optional: `valid_from`, `review_by` (date to re-verify perishable facts),
`supersedes`, `superseded_by`, `sensitivity` (required in people/ and life/:
normal | personal; `private` content goes to vault/, never plaintext).

One fact/decision per note. Bodies use absolute dates only ("2026-07-22",
never "today" or "last week"). Link related notes with [[note-id]] wikilinks.

Wikilinks are load-bearing, not decoration: lint REJECTS a link to an id that
does not exist, and warns when one points at a superseded note (telling you the
successor to repoint at). They also build the backlink graph, so linking a new
note into its neighbours is what makes it findable by relationship later.

## What earns a note — capture policy

Two tiers, two different bars. `inbox/` and `journal/` sit outside default
search, so a wrong guess there is nearly free — consolidation deletes what
turns out to be noise. The canonical folders ARE the search results, so a junk
note there costs forever: it ranks in every future query and gets read back as
true. **Capture generously, promote strictly.**

The one-line test: **capture the why, not the what.** The what is recoverable
from git, files and calendars. The why evaporates within a week and is gone.

Capture without asking (`bin/brain capture` → inbox/):

- a decision AND its reasoning — above all, the alternatives that were rejected
- a stated preference or constraint that should shape later work
- a fact about your world that could not be inferred: people, tools,
  obligations, environment quirks, prices, commitments
- an answer that cost real effort — a root cause, a non-obvious config, a dead
  end worth not walking into twice

Never capture:

- anything derivable from the code, this repo, or public docs — link, don't copy
- transient state ("the build is failing", "waiting on their reply")
- credentials of any kind (hard rule below)
- praise, chatter, or a restatement of what was just done

Ask first, every single time — never silently:

- anything about a named person's private life, health, finances or
  relationships. This repo auto-pushes to GitHub. `sensitivity` is decided at
  capture time, or the content goes to vault/ — never retrofitted afterwards.

Promotion bar (inbox → canonical, applied by consolidation): would you
search for this, and would a stale version of it mislead you? If it is not
worth maintaining for ten years, delete it instead of promoting it.

Offer, don't nag. When something above passes the bar mid-conversation, say so
in one line and carry on — do not save the offer for the end of the session,
because by then the reasoning is already gone. "Remember this" from you
overrides every rule here and captures immediately.

## Writing notes — created and edited correctly

1. Create via `bin/brain new <kind> "<title>" --topics a,b` — never hand-roll
   frontmatter. Quick thoughts: `bin/brain capture "..."` → inbox/.
2. Fill the body and the aliases list.
3. Run `bin/brain lint` and fix EVERY error before committing. Warnings are
   advisory but usually worth fixing on the spot.
4. Commit when done (small, frequent commits). The pre-commit hook re-runs
   lint and blocks bad content; post-commit auto-pushes to GitHub.
5. New topic needed? Add one line to `knowledge/topics.yaml` in the same
   commit — that is deliberate, not friction.

## Superseding — when a decision or fact changes

Never edit the old note's meaning and never delete it. Preferred path:

```
bin/brain supersede <old-id> "<new title>"
```

does everything at once: creates the successor with `supersedes` set, marks
the old note `status: superseded` + `superseded_by`, stamps the banner, and
moves it to `archive/`. Then fill the new body, replace the banner's
`<one-line reason>`, lint, commit. (Manual equivalent, if ever needed:
those same four steps by hand — lint verifies the chain either way and
blocks a half-done supersede.)

This is why retrieval stays precise: superseded notes physically leave the
default search scope.

## Searching — tier protocol

0. Entry points: `knowledge/index.md`, then the folder that fits.
1. `rg` from the repo root — `.rgignore` excludes archive/, vault/, journal/
   and inbox/, so plain grep is CURRENT CANONICAL by construction.
2. `bin/brain search "<query>"` — FTS5 with stemming, alias boosting, and
   folder-weighted ranking (index auto-rebuilds when stale). `--scope all`
   adds journal + inbox, tagged provisional. Same tools over MCP from any
   session: brain_search / brain_read / brain_recent / brain_capture.
3. Before declaring a miss, rewrite the query into 2-3 lexical variants
   (synonyms, singular/plural, the term you'd have used when writing it).
4. `bin/brain read <id-or-path>` resolves supersede chains — use it whenever
   a note mentions supersedes/superseded_by, so you always land on current.
   It also prints `linked from:` — the notes that reference this one.
4b. `bin/brain links <id>` (MCP: `brain_links`) walks the [[wikilink]] graph:
   what points AT a note, and what it points to. Search finds notes by
   wording; links finds them by relationship. Use it when the question is
   "everything about X" or when one good hit probably has neighbours — the
   related note often never repeats the search term.
5. `journal/` and `inbox/` are opt-in mechanically (excluded by .rgignore) and
   hold PROVISIONAL material that has not been through consolidation — for
   time-flavored questions ("what was I doing in March") search them
   explicitly: `rg --no-ignore <term> knowledge/journal/` or
   `bin/brain search --scope all`. Never present an inbox item as settled.
6. `archive/` is opt-in — search it only for history questions ("what did I
   previously decide", "why did this change"). Never present archived content
   as current; always mention it was superseded and by what.
7. When you answer from a note, cite it by path so the answer is auditable.

## Hard rules

- NO credentials, tokens, or keys anywhere in this repo — lint and the hooks
  enforce this, but do not test them. Values go to macOS Keychain;
  `.env.example` documents names only.
- Sensitive personal content (`sensitivity: private`) only as encrypted .age
  files in vault/ — see `setup/runbooks/vault.md`.
- Never rewrite archived note bodies (append-only history).
- Never store derived indexes as if they were truth; they are rebuilt from
  the files, never the reverse.
- Health check: `bin/brain doctor` (hooks, backup freshness, lint status).
