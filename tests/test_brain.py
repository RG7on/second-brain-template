#!/usr/bin/env python3
"""Runtime tests for the brain toolbelt — stdlib unittest, no dependencies.

These cover BEHAVIOR, which lint cannot: that the bootstrap command actually
runs on a clean clone, that brain_read refuses to leave knowledge/, and that
one malformed MCP request cannot take the server down.

Anything that WRITES runs against a throwaway copy of the repo with HOME
redirected (see make_sandbox) — a test must never be able to damage the real
brain, and must never leave it in a state that blocks a commit.

Run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "bin" / "brain"
MCP = ROOT / "bin" / "brain-mcp"
K = ROOT / "knowledge"

SANDBOX_IGNORE = shutil.ignore_patterns(
    ".git", ".cache", "graphify-out", "node_modules", ".DS_Store")


def temp_dir():
    """A sandbox whose teardown tolerates the detached post-commit job.

    post-commit reindexes and pushes in a fully detached subshell (it must not
    hold the caller's stdout pipe open, or every capture would block on the
    network). That job can still be writing into the sandbox's .cache/ as the
    test tears it down, which is a harmless race but a fatal rmtree."""
    try:
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except TypeError:                      # Python 3.9 has no such argument
        return tempfile.TemporaryDirectory()


def cleanup_temp(tmp):
    try:
        tmp.cleanup()
    except OSError:
        pass


def run_brain(*args, repo=None):
    root = repo or ROOT
    return subprocess.run([sys.executable, str(root / "bin" / "brain"), *args],
                          cwd=root, capture_output=True, text=True, timeout=180)


def make_sandbox(tmp):
    """A throwaway clone-alike. The two machine-local generated files are
    removed so this matches what a REAL fresh clone looks like: they are
    gitignored, so they never arrive with the clone."""
    repo = Path(tmp) / "repo"
    shutil.copytree(ROOT, repo, symlinks=True, ignore=SANDBOX_IGNORE)
    for generated in (repo / ".mcp.json", repo / "setup/skills/brain/SKILL.md"):
        if generated.exists():
            generated.unlink()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    # Pre-set the hooks path so ensure_hooks() stays quiet: otherwise it prints
    # an install line ahead of --json output on the first command.
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo,
                   check=True, capture_output=True)
    return repo


def json_payload(stdout):
    """Parse --json output, tolerating any advisory line printed before it."""
    start = stdout.find("{")
    if start == -1:
        raise AssertionError(f"no JSON in output:\n{stdout}")
    return json.loads(stdout[start:])


class ReadContainmentTests(unittest.TestCase):
    """brain_read is reachable by any Claude session over MCP — it must never
    return a file from outside knowledge/, however the path is spelled.
    Read-only: these run against the real repo."""

    def assert_refused(self, request, forbidden_marker, repo=None):
        result = run_brain("read", request, repo=repo)
        self.assertEqual(result.returncode, 1,
                         f"{request!r} should be refused, got rc={result.returncode}")
        self.assertNotIn(forbidden_marker, result.stdout,
                         f"{request!r} leaked content from outside knowledge/")

    def test_parent_traversal_refused(self):
        # README.md sits in the repo root — inside the repo, outside knowledge/.
        self.assert_refused("knowledge/../README.md", "permanent second brain")

    def test_traversal_through_real_subfolder_refused(self):
        self.assert_refused("knowledge/decisions/../../README.md", "permanent second brain")

    def test_absolute_path_outside_knowledge_refused(self):
        self.assert_refused(str(ROOT / "README.md"), "permanent second brain")

    def test_escape_to_an_existing_file_outside_the_repo_refused(self):
        """Depth-independent: the target is a file this test creates, so the
        refusal can only come from containment — never from 'no such file'.
        (A fixed ../../../../etc/hosts probe is vacuous wherever the repo
        happens to sit at a different depth, e.g. on a CI runner.)"""
        with tempfile.TemporaryDirectory() as td:
            secret = Path(td) / "secret.md"
            secret.write_text("TOPSECRET-CANARY\n", encoding="utf-8")
            hops = os.path.relpath(secret, K)          # e.g. ../../../tmp/x/secret.md
            self.assertTrue(hops.startswith(".."), "probe must point outside knowledge/")
            self.assert_refused(hops, "TOPSECRET-CANARY")
            self.assert_refused(f"knowledge/{hops}", "TOPSECRET-CANARY")

    def test_legitimate_note_still_readable(self):
        """The fix must not break the thing the tool is for."""
        result = run_brain("read", "knowledge/index.md")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), "index.md read returned nothing")

    def test_nonexistent_note_reports_missing(self):
        result = run_brain("read", "knowledge/decisions/does-not-exist.md")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stdout)

    def test_non_markdown_file_inside_knowledge_refused(self):
        """`brain read` serves NOTES — .md only — and that is deliberate, not a
        side effect of the containment fix. topics.yaml is the one non-.md file
        under knowledge/, it exists, and it is still refused; this test exists so
        that widening the suffix rule is a decision someone makes on purpose.

        Nothing is lost: topics.yaml is a normal file any session can open. Only
        the MCP-reachable surface is kept at exactly one file type."""
        target = K / "topics.yaml"
        self.assertTrue(target.is_file(),
                        "probe is vacuous unless topics.yaml actually exists")
        result = run_brain("read", "knowledge/topics.yaml")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Controlled topic vocabulary", result.stdout,
                         "brain read served a non-.md file")


class SandboxContainmentTests(unittest.TestCase):
    """Containment cases that need to PLANT files inside knowledge/. Planting a
    symlink in the real tree would make bin/brain lint fail and block commits
    if the run were interrupted, so these use a sandbox copy."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.outside = Path(self.tmp.name) / "outside.md"
        self.outside.write_text("TOPSECRET-CANARY\n", encoding="utf-8")

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_symlink_inside_knowledge_cannot_escape(self):
        link = self.repo / "knowledge" / "reference" / "probe.md"
        link.symlink_to(self.outside)
        result = run_brain("read", "knowledge/reference/probe.md", repo=self.repo)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("TOPSECRET-CANARY", result.stdout)

    def test_symlinked_note_cannot_escape_via_its_id(self):
        """The id lookup reads the file off disk too — a symlink with valid
        frontmatter must not turn `brain read <id>` into an exfil route."""
        self.outside.write_text(
            "---\nid: probe-note\nkind: reference\ntitle: Probe\n"
            "topics: [brain]\ncreated: 2026-07-22\nstatus: current\n---\n\n"
            "TOPSECRET-CANARY\n", encoding="utf-8")
        (self.repo / "knowledge" / "reference" / "probe.md").symlink_to(self.outside)
        result = run_brain("read", "probe-note", repo=self.repo)
        self.assertNotIn("TOPSECRET-CANARY", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout)


class McpServerTests(unittest.TestCase):
    """One bad request must not kill a server that other sessions are sharing."""

    def rpc(self, *requests, raw=None):
        payload = raw if raw is not None else "".join(
            json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run([sys.executable, str(MCP)], input=payload, cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        replies = {}
        for line in proc.stdout.splitlines():
            if line.strip():
                replies[json.loads(line).get("id")] = json.loads(line)
        return proc, replies

    @staticmethod
    def call(msg_id, tool, arguments):
        return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}

    PING = {"jsonrpc": "2.0", "id": 99, "method": "ping"}

    def assert_survived_with_tool_error(self, bad_call):
        proc, replies = self.rpc(bad_call, self.PING)
        self.assertIn(99, replies, "server died — the follow-up ping was never answered\n"
                                   f"stderr: {proc.stderr[-600:]}")
        self.assertIn(1, replies, "bad call got no reply at all")
        self.assertTrue(replies[1].get("result", {}).get("isError"),
                        f"expected a tool error, got {replies[1]}")
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_required_arg(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", {}))

    def test_missing_required_arg_read(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_read", {}))

    def test_missing_required_arg_capture(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_capture", {}))

    def test_wrong_type_arg(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", {"query": 123}))

    def test_null_required_arg(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", {"query": None}))

    def test_arguments_not_an_object(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", ["query"]))

    def test_oversized_arg(self):
        self.assert_survived_with_tool_error(
            self.call(1, "brain_search", {"query": "x" * 200_001}))

    def test_bad_enum_value(self):
        self.assert_survived_with_tool_error(
            self.call(1, "brain_search", {"query": "test", "scope": "everything"}))

    def test_unknown_tool(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_nonexistent", {}))

    def test_non_string_tool_name(self):
        self.assert_survived_with_tool_error(self.call(1, {"weird": True}, {}))

    def test_deeply_nested_json_does_not_kill_the_server(self):
        """json.loads raises RecursionError — NOT JSONDecodeError — on deeply
        nested input. A guard that catches only JSONDecodeError lets one
        request take down every session sharing this server."""
        proc, replies = self.rpc(
            raw="[" * 100_000 + "]" * 100_000 + "\n" + json.dumps(self.PING) + "\n")
        self.assertIn(99, replies,
                      f"server died on nested JSON\nstderr: {proc.stderr[-400:]}")

    def test_absurdly_long_line_does_not_kill_the_server(self):
        proc, replies = self.rpc(raw='"' + "x" * 20_000_000 + '"\n'
                                 + json.dumps(self.PING) + "\n")
        self.assertIn(99, replies, "server died on an oversized line")

    def test_params_not_an_object(self):
        proc, replies = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "nope"}, self.PING)
        self.assertIn(99, replies, "server died on non-object params")

    def test_optional_null_is_treated_as_absent(self):
        """An explicit null for an optional arg is a normal thing for a model
        to emit; it must not fail the search."""
        _proc, replies = self.rpc(
            self.call(1, "brain_search", {"query": "brain", "scope": None, "limit": None}))
        self.assertFalse(replies[1]["result"]["isError"],
                         f"null optionals should be ignored, got {replies[1]}")

    def test_integer_sent_as_digit_string_is_accepted(self):
        _proc, replies = self.rpc(
            self.call(1, "brain_search", {"query": "brain", "limit": "3"}))
        self.assertFalse(replies[1]["result"]["isError"],
                         f'limit "3" should be coerced, got {replies[1]}')

    def test_tools_list_contract(self):
        _proc, replies = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertEqual(names, {"brain_search", "brain_read", "brain_links",
                                 "brain_recent", "brain_capture"})

    def test_malformed_json_line_is_skipped(self):
        _proc, replies = self.rpc(raw="{not json at all\n[1,2,3]\n"
                                  + json.dumps(self.PING) + "\n")
        self.assertIn(99, replies, "server did not survive malformed input lines")

    def test_read_over_mcp_cannot_traverse(self):
        """The containment fix must hold through the MCP surface too."""
        _proc, replies = self.rpc(
            self.call(1, "brain_read", {"id_or_path": "knowledge/../README.md"}))
        self.assertNotIn("permanent second brain",
                         replies[1]["result"]["content"][0]["text"])


class InitTests(unittest.TestCase):
    """Bootstrap has to work on a machine that has never run this repo before —
    that is the whole disaster-recovery story, so it gets an isolated clone,
    an isolated HOME, and a stubbed claude CLI (a test must never be able to
    rewrite the developer's own MCP registration)."""

    def setUp(self):
        self.tmp = temp_dir()
        tmp = Path(self.tmp.name)
        self.repo = make_sandbox(tmp)
        self.home = tmp / "home"
        self.stub_bin = tmp / "stubbin"
        self.home.mkdir()
        self.stub_bin.mkdir()
        self.calls = self.stub_bin / "calls.log"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def stub_claude(self, scope=None, command=None, add_exit=0, add_message="",
                    get_output=None):
        """Fake the `claude` CLI as a CLI — not as whatever subcommand cmd_init
        happens to call today.

        `mcp list` and `mcp get` BOTH answer, consistently, in the real CLI's
        formats. That matters: a stub that only knows the subcommand the current
        code calls silently passes against an implementation that asks the other
        one, which is exactly how a regression test turns vacuous.

        scope=None means nothing is registered anywhere. Every invocation is
        logged so a test can assert what init actually ran, not what it printed.
        """
        command = command or self.server_path()
        if scope == "user":
            get_body = ("brain:\n"
                        "  Scope: User config (available in all your projects)\n"
                        "  Status: ✔ Connected\n"
                        "  Type: stdio\n"
                        f"  Command: {command}\n"
                        "  Args:\n"
                        "  Environment:\n")
            list_body = f"  brain: {command}  - ✔ Connected"
        elif scope == "project":
            # What the CLI reports when the ONLY entry is the .mcp.json that
            # init's own step 2 wrote moments earlier.
            get_body = ("brain:\n"
                        "  Scope: Project config (shared via .mcp.json)\n"
                        "  Status: ⏸ Pending approval (run `claude` to approve)\n")
            list_body = f"  brain: {command}  - ⏸ Pending approval (run `claude` to approve)"
        else:
            get_body = 'No MCP server named "brain". Run `claude mcp add` to add one.'
            list_body = ""
        if get_output is not None:
            get_body = get_output
        stub = self.stub_bin / "claude"
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.calls}"\n'
            'if [ "$2" = "list" ]; then\n'
            f"cat <<'ENDLIST'\n{list_body}\nENDLIST\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$2" = "get" ]; then\n'
            f"cat <<'ENDGET'\n{get_body}\nENDGET\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$2" = "add" ]; then\n'
            f"  echo '{add_message}' >&2\n"
            f"  exit {add_exit}\n"
            "fi\n"
            "exit 0\n", encoding="utf-8")
        stub.chmod(0o755)

    def claude_calls(self):
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

    def env_with(self, claude=True):
        git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
        path = f"{self.stub_bin}:{git_dir}:/usr/bin:/bin" if claude \
            else f"{git_dir}:/usr/bin:/bin"
        if not claude:
            self.assertIsNone(shutil.which("claude", path=path),
                              "test setup: claude still reachable on PATH")
        return {**os.environ, "HOME": str(self.home), "PATH": path,
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "XDG_CONFIG_HOME": str(self.home / ".config")}

    def init(self, env=None):
        return subprocess.run([sys.executable, str(self.repo / "bin" / "brain"), "init"],
                              cwd=self.repo, capture_output=True, text=True,
                              env=env or self.env_with(), timeout=180)

    def server_path(self):
        return str(self.repo.resolve() / "bin" / "brain-mcp")

    def test_init_wires_a_clean_clone_and_is_idempotent(self):
        self.stub_claude()                       # nothing registered yet
        first = self.init()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        # /var is a symlink to /private/var on macOS, so compare resolved.
        repo = self.repo.resolve()
        mcp = json.loads((self.repo / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp["mcpServers"]["brain"]["command"], self.server_path())

        skill = (self.repo / "setup/skills/brain/SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("{{REPO}}", skill, "template placeholder left unrendered")
        self.assertIn(str(repo), skill, "skill not pointed at this clone")

        link = self.home / ".claude" / "skills" / "brain"
        self.assertTrue(link.is_symlink(), "/brain skill was not linked into ~/.claude/skills")
        self.assertEqual(link.resolve(), (self.repo / "setup/skills/brain").resolve())

        hooks = subprocess.run(["git", "config", "core.hooksPath"], cwd=self.repo,
                               capture_output=True, text=True)
        self.assertEqual(hooks.stdout.strip(), ".githooks")

        # Idempotent: now the stub reports it as registered at THIS path.
        self.stub_claude(scope="user")
        second = self.init()
        self.assertEqual(second.returncode, 0,
                         "re-running init must be safe\n" + second.stdout + second.stderr)

    def test_stale_registration_is_reported_not_called_success(self):
        """`claude mcp add` refuses with 'already exists' whether or not the
        registered path is this clone, so trusting that message reports success
        while every session outside the repo is wired to a dead path."""
        self.stub_claude(
            scope="user", command="/somewhere/else/bin/brain-mcp",
            add_exit=1, add_message="MCP server brain already exists in user config")
        result = self.init()
        self.assertEqual(result.returncode, 1,
                         "a stale registration must not be reported as success\n"
                         + result.stdout)
        self.assertIn("claude mcp remove brain -s user", result.stdout,
                      "the repair command must be printed")

    def test_matching_registration_is_success(self):
        self.stub_claude(scope="user")
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("mcp add", self.claude_calls(),
                         "init re-registered a server that was already correct")

    def test_project_scoped_entry_does_not_count_as_registered(self):
        """Step 2 writes a project-scoped .mcp.json, so asking `claude mcp list`
        shows brain as present on a machine that has NEVER registered it at user
        scope — init's own side effect answering init's own question. Believing
        it leaves brain tools working inside this repo and nowhere else, which
        is the single thing step 5 exists to prevent."""
        self.stub_claude(scope="project")
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mcp add --scope user brain", self.claude_calls(),
                      "a project-scoped entry was mistaken for a user-scope one, so the "
                      "user-scope registration never happened")

    def test_unverifiable_user_scope_entry_is_not_called_success(self):
        """A user-scope entry whose command cannot be read cannot be confirmed
        to point at this clone. Unverifiable is not the same as fine."""
        self.stub_claude(scope="user", get_output=(
            "brain:\n"
            "  Scope: User config (available in all your projects)\n"
            "  Status: ✔ Connected\n"))
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("claude mcp remove brain -s user", result.stdout)

    def test_init_reports_failure_when_template_missing(self):
        """A silent partial bootstrap is what broke this command before —
        a missing template must surface as a nonzero exit, not a cheery one."""
        self.stub_claude()
        (self.repo / "setup/skills/brain/SKILL.md.template").unlink()
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SKILL.md.template", result.stdout)

    def test_no_dangling_skill_link_when_there_is_no_skill(self):
        """Linking ~/.claude/skills/brain at a directory with no SKILL.md gives
        Claude a broken skill — worse than no link."""
        self.stub_claude()
        (self.repo / "setup/skills/brain/SKILL.md.template").unlink()
        self.init()
        self.assertFalse((self.home / ".claude" / "skills" / "brain").exists(),
                         "init planted a link to a skill that does not exist")

    def test_real_registration_failure_is_reported(self):
        self.stub_claude(add_exit=1, add_message="connection refused")
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("connection refused", result.stdout)

    def test_init_defers_when_claude_cli_absent(self):
        """No Claude CLI is not a failure — init prints the exact command."""
        result = self.init(env=self.env_with(claude=False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claude mcp add --scope user brain", result.stdout)
        self.assertTrue((self.repo / ".mcp.json").is_file(),
                        "a missing claude CLI must not block the rest of the wiring")

    def test_init_preserves_other_mcp_servers(self):
        """.mcp.json is gitignored now, so clobbering someone else's entry here
        destroys it with no way to get it back."""
        self.stub_claude()
        (self.repo / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"other": {"command": "/usr/local/bin/other-mcp", "args": []}}
        }), encoding="utf-8")
        self.init()
        servers = json.loads((self.repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        self.assertIn("other", servers, "init destroyed an unrelated MCP server")
        self.assertEqual(servers["other"]["command"], "/usr/local/bin/other-mcp")
        self.assertEqual(servers["brain"]["command"], self.server_path())

    def test_init_does_not_clobber_a_real_skills_directory(self):
        """If ~/.claude/skills/brain is a real directory, init must refuse to
        delete it rather than silently destroying whatever is there."""
        self.stub_claude()
        real_dir = self.home / ".claude" / "skills" / "brain"
        real_dir.mkdir(parents=True)
        (real_dir / "keepme.txt").write_text("precious", encoding="utf-8")
        result = self.init()
        self.assertIn("is not a symlink", result.stdout,
                      "init must say why it skipped, not just fail")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue((real_dir / "keepme.txt").is_file(), "init destroyed existing content")

    def test_init_reports_failure_outside_a_git_repo(self):
        """Claiming the commit gate is installed when git rejected the config
        is exactly the kind of false success this command used to give."""
        self.stub_claude()
        shutil.rmtree(self.repo / ".git")
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("core.hooksPath", result.stdout)

    def test_doctor_runs_on_a_fresh_clone(self):
        """doctor must produce a report, not a traceback, on a fresh clone."""
        self.stub_claude()
        self.assertEqual(self.init().returncode, 0)
        result = subprocess.run([sys.executable, str(self.repo / "bin" / "brain"), "doctor"],
                                cwd=self.repo, capture_output=True, text=True,
                                env=self.env_with(), timeout=300)
        self.assertIn("brain doctor", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_capture_writes_the_note_and_prints_its_path(self):
        result = run_brain("capture", "test capture probe", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # ensure_hooks() may print first on a fresh repo — the note path is
        # the last line capture prints without --commit.
        written = Path(result.stdout.strip().splitlines()[-1])
        self.assertTrue(written.is_file(), f"no file at {written}\n{result.stdout}")
        self.assertIn("test capture probe", written.read_text(encoding="utf-8"))

    def test_failed_commit_is_reported_as_failure(self):
        """--commit was asked for; if git refused, the note is NOT backed up.
        Returning 0 made the MCP layer report isError:false and every caller
        believe the note was safely in git."""
        hook = self.repo / ".githooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'gate says no' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        result = run_brain("capture", "probe that cannot commit", "--commit", repo=self.repo)
        self.assertEqual(result.returncode, 1,
                         "a refused commit must not be reported as success\n" + result.stdout)
        self.assertIn("NOT COMMITTED", result.stdout)
        # ...and the note must still be on disk, so nobody re-captures it.
        notes = list((self.repo / "knowledge" / "inbox").glob("*probe-that-cannot-commit*.md"))
        self.assertTrue(notes, "the capture was lost entirely")


class SearchRankingTests(unittest.TestCase):
    """FTS5 returns matches in an arbitrary order without ORDER BY. Ranking in
    Python after `LIMIT 100` therefore threw away the best hit as soon as more
    than 100 notes matched."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write_note(self, name, title, body):
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {title}\n"
            f"topics: [brain]\ncreated: 2026-07-22\nstatus: current\n---\n\n{body}\n",
            encoding="utf-8")

    def test_best_hit_survives_more_than_100_matches(self):
        # 150 notes mention the term only in the body...
        for i in range(150):
            self.write_note(f"filler-{i:03d}", f"Filler {i}", "passing mention of wumpus here")
        # ...one has it in the title, which is weighted 8x. It must rank first.
        self.write_note("the-wumpus-note", "Wumpus", "wumpus wumpus wumpus")
        result = run_brain("search", "wumpus", "--limit", "3", "--json", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hits = json_payload(result.stdout)["hits"]
        self.assertTrue(hits, "no hits at all")
        self.assertEqual(hits[0]["id"], "the-wumpus-note",
                         f"best hit lost among 151 matches; got {[h['id'] for h in hits]}")

    def test_deleting_a_note_removes_it_from_search(self):
        """Deletion changes no surviving file's mtime, so an mtime-only
        freshness check leaves the note answering searches forever."""
        self.write_note("doomed-note", "Doomed", "zzyzx unique marker")
        first = run_brain("search", "zzyzx", "--json", repo=self.repo)
        self.assertTrue(json_payload(first.stdout)["hits"], "setup: note was not indexed")
        (self.ref / "doomed-note.md").unlink()
        after = run_brain("search", "zzyzx", "--json", repo=self.repo)
        self.assertEqual(json_payload(after.stdout)["hits"], [],
                         "deleted note is still in the index")

    def test_query_starting_with_dashes_is_not_parsed_as_a_flag(self):
        """Options come before `--`; everything after it is query text."""
        self.write_note("flag-note", "Flag discussion", "a note about --zzyzx-flag handling")
        result = run_brain("search", "--json", "--", "--zzyzx-flag", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hits = json_payload(result.stdout)["hits"]
        self.assertTrue(hits, "a query beginning with -- was swallowed as a flag")
        self.assertEqual(hits[0]["id"], "flag-note")

    def test_concurrent_first_searches_all_succeed(self):
        """Every Claude session runs its own MCP server and each rebuilds on
        its first stale search — they used to race on one shared temp file."""
        import concurrent.futures
        self.write_note("race-note", "Race", "concurrency marker term")
        index = self.repo / ".cache" / "index.db"
        if index.exists():
            index.unlink()                      # force all 20 to find it stale
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            runs = [pool.submit(run_brain, "search", "concurrency", "--json", repo=self.repo)
                    for _ in range(20)]
            results = [r.result() for r in runs]
        for r in results:
            self.assertEqual(r.returncode, 0, f"concurrent search failed:\n{r.stderr[-400:]}")
        payloads = {json.dumps(json_payload(r.stdout)["hits"]) for r in results}
        self.assertEqual(len(payloads), 1, "concurrent searches disagreed on results")


class ConsolidateTests(unittest.TestCase):
    """Consolidation ran its commit and push from a `finally:`, so a model that
    failed still got its work committed and pushed, and the closing message
    said it landed regardless."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.stub_bin = Path(self.tmp.name) / "stubbin"
        self.stub_bin.mkdir()
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def stub_claude(self, body):
        stub = self.stub_bin / "claude"
        stub.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        stub.chmod(0o755)

    def consolidate(self):
        git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
        env = {**os.environ, "PATH": f"{self.stub_bin}:{git_dir}:/usr/bin:/bin"}
        return subprocess.run([sys.executable, str(self.repo / "bin" / "brain"), "consolidate"],
                              cwd=self.repo, capture_output=True, text=True,
                              env=env, timeout=300)

    def commits_on_branch(self):
        out = subprocess.run(["git", "log", "--oneline", "--all"], cwd=self.repo,
                             capture_output=True, text=True)
        return [l for l in out.stdout.splitlines() if "consolidate:" in l]

    def test_model_failure_does_not_commit_or_claim_success(self):
        # The model "runs", writes a change, then fails.
        self.stub_claude('echo "partial work" >> knowledge/inbox/half-done.md\nexit 1')
        result = self.consolidate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DID NOT LAND", result.stdout)
        self.assertEqual(self.commits_on_branch(), [],
                         "a failed consolidation was committed anyway")

    def test_model_timeout_or_crash_is_reported(self):
        self.stub_claude("exit 3")
        result = self.consolidate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude exited 3", result.stdout)

    def test_clean_run_with_no_changes_reports_no_changes(self):
        self.stub_claude("exit 0")
        result = self.consolidate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no changes", result.stdout)


class SessionDigestTests(unittest.TestCase):
    """Session mining reads transcripts from OUTSIDE the repo and hands them to
    the consolidation model, so three things have to hold: only the user's own
    words are extracted (mining my own output back in would launder my
    speculation into his recorded fact), the window follows when he actually
    talked rather than when a file was touched, and the digest never becomes
    committable."""

    def setUp(self):
        self.tmp = temp_dir()
        tmp = Path(self.tmp.name)
        self.repo = make_sandbox(tmp)
        self.home = tmp / "home"
        self.projects = self.home / ".claude" / "projects" / "-home-example-proj"
        self.projects.mkdir(parents=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def iso(self, days_ago):
        stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S")

    def user_turn(self, text, ts, **extra):
        return {"type": "user", "timestamp": ts, "cwd": "/home/example/proj",
                "message": {"role": "user", "content": text}, **extra}

    def write_transcript(self, name, records):
        path = self.projects / name
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return path

    def sessions(self, *args):
        return subprocess.run(
            [sys.executable, str(self.repo / "bin" / "brain"), "sessions", *args],
            cwd=self.repo, capture_output=True, text=True, timeout=180,
            env={**os.environ, "HOME": str(self.home)})

    def test_extracts_only_the_users_own_prompts(self):
        self.write_transcript("a.jsonl", [
            self.user_turn("KEEPME dropped Redis because the ops cost was real",
                           self.iso(1)),
            self.user_turn("SIDECHAINLEAK", self.iso(1), isSidechain=True),
            self.user_turn("<local-command-stdout>CMDLEAK</local-command-stdout>",
                           self.iso(1)),
            {"type": "user", "timestamp": self.iso(1),
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "content": "TOOLLEAK"}]}},
            {"type": "assistant", "timestamp": self.iso(1),
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "ASSISTANTLEAK"}]}},
            self.user_turn("kept <system-reminder>REMINDERLEAK</system-reminder> text",
                           self.iso(1)),
        ])
        out = self.sessions("--stdout")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("KEEPME dropped Redis", out.stdout)
        for leak in ("SIDECHAINLEAK", "CMDLEAK", "TOOLLEAK",
                     "ASSISTANTLEAK", "REMINDERLEAK"):
            self.assertNotIn(leak, out.stdout, f"{leak} leaked into the digest")

    def test_window_follows_when_he_talked_not_file_mtime(self):
        # Both files are written now, so both have a fresh mtime. Only the
        # recent CONVERSATION may be mined: a months-old session in a file that
        # was merely reindexed or copied would otherwise resurface every week.
        self.write_transcript("old.jsonl", [self.user_turn("ANCIENTTHOUGHT", self.iso(90))])
        self.write_transcript("new.jsonl", [self.user_turn("RECENTTHOUGHT", self.iso(1))])
        out = self.sessions("--stdout")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("RECENTTHOUGHT", out.stdout)
        self.assertNotIn("ANCIENTTHOUGHT", out.stdout,
                         "an old session was mined because its file was touched recently")

    def test_embedded_headings_cannot_forge_a_session_boundary(self):
        self.write_transcript("a.jsonl", [
            self.user_turn("## 2026-01-01 — /fake/dir\nprose after a heading", self.iso(1))])
        out = self.sessions("--stdout")
        self.assertEqual(out.stdout.count("\n## "), 1,
                         "a prompt's own heading was counted as a session header")

    def test_digest_is_written_to_cache_and_never_committable(self):
        self.write_transcript("a.jsonl", [self.user_turn("PRIVATEMUSING", self.iso(1))])
        self.assertEqual(self.sessions().returncode, 0)
        digest = self.repo / ".cache" / "session-digest.md"
        self.assertTrue(digest.exists(), "no digest was written")
        self.assertIn("PRIVATEMUSING", digest.read_text(encoding="utf-8"))
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                                capture_output=True, text=True).stdout
        self.assertNotIn("session-digest", status,
                         "the digest is git-visible — mined session text could be committed")

    def test_missing_transcript_root_is_not_an_error(self):
        shutil.rmtree(self.home / ".claude")
        out = self.sessions("--stdout")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing to mine", out.stdout)


class SupersedeTests(unittest.TestCase):
    """The two note-creating commands had no coverage. supersede in particular
    could overwrite an existing note (blank template → data loss) and rewrite
    append-only archive history."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write_ref(self, name, title, body, **fm):
        extra = "".join(f"{k}: {v}\n" for k, v in fm.items())
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {title}\n"
            f"topics: [brain]\naliases: [{name}]\ncreated: 2026-07-01\n"
            f"status: current\nreview_by: null\n{extra}---\n\n{body}\n",
            encoding="utf-8")

    def seed(self, name="seed-note"):
        """A note this test owns, to supersede. Tests must never depend on a
        note that happens to exist in the repo — the published template ships
        an EMPTY knowledge/, and a test keyed to the author's notes either
        fails there or, worse, passes vacuously."""
        self.write_ref(name, "Seed note", "The original claim.")
        return name

    def test_supersede_refuses_to_overwrite_an_existing_file(self):
        """A note whose filename != its id (legal) must not be clobbered when a
        supersede title happens to slugify onto its path."""
        # file api-limits.md holds id vendor-api-limits and a real fact
        (self.ref / "api-limits.md").write_text(
            "---\nid: vendor-api-limits\nkind: reference\ntitle: Vendor API limits\n"
            "topics: [brain]\naliases: [rate, quota]\ncreated: 2026-07-01\n"
            "status: current\nreview_by: null\n---\n\nThe vendor rate limit is 500 req/min.\n",
            encoding="utf-8")
        # supersede a note of our own with a title slugifying to "api-limits"
        seed = self.seed()
        result = run_brain("supersede", seed, "API limits", repo=self.repo)
        self.assertEqual(result.returncode, 1, "supersede overwrote an existing note\n" + result.stdout)
        self.assertIn("500 req/min", (self.ref / "api-limits.md").read_text(encoding="utf-8"),
                      "the pre-existing note's body was destroyed")

    def test_supersede_produces_a_lint_clean_tree(self):
        """The happy path must leave the repo committable."""
        seed = self.seed()
        result = run_brain("supersede", seed, "Seed note v2", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lint = run_brain("lint", repo=self.repo)
        self.assertEqual(lint.returncode, 0, "supersede left the tree un-lintable:\n" + lint.stdout)
        # old note left canonical folder; successor is current and points back
        self.assertFalse((self.repo / f"knowledge/reference/{seed}.md").exists())
        self.assertTrue((self.repo / f"knowledge/archive/reference/{seed}.md").exists())


class LintIntegrityTests(unittest.TestCase):
    """Knowledge-integrity checks that keep superseded/contradictory content
    from being served as one current fact."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write(self, name, extra="", body="ok"):
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {name}\ntopics: [brain]\n"
            f"aliases: [{name}]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"
            f"{extra}---\n\n{body}\n", encoding="utf-8")

    def test_superseded_by_on_a_current_note_is_rejected(self):
        self.write("zombie", extra="superseded_by: something\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("must not carry superseded_by", out.stdout)

    def test_merge_conflict_markers_are_rejected(self):
        self.write("conflicted",
                   body="## What\n<<<<<<< HEAD\n500 req/min\n=======\n1000 req/min\n>>>>>>> other\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("merge-conflict", out.stdout)

    def test_setext_heading_is_not_flagged_as_a_conflict(self):
        """A markdown H1 underline (a run of '=') must not false-positive."""
        self.write("heading", body="Real Heading\n=======\nbody text here\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0, "setext underline false-flagged:\n" + out.stdout)

    def test_forked_supersede_is_rejected(self):
        arch = self.repo / "knowledge/archive/reference"
        arch.mkdir(parents=True, exist_ok=True)
        (arch / "old.md").write_text(
            "---\nid: old-fact\nkind: reference\ntitle: Old\ntopics: [brain]\n"
            "aliases: [old]\ncreated: 2026-06-01\nstatus: superseded\n"
            "superseded_by: new-a\nreview_by: null\n---\n\n> SUPERSEDED 2026-07-01 by [[new-a]] — x\n",
            encoding="utf-8")
        for n in ("a", "b"):
            self.write(f"new-{n}", extra="supersedes: old-fact\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1, "two notes superseding one target passed lint")
        self.assertIn("claim to supersede", out.stdout)


class CaptureConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def _committed(self):
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.repo,
                             capture_output=True, text=True).stdout
        return log.count("capture:")

    def _uncommitted(self):
        st = subprocess.run(["git", "status", "--porcelain", "knowledge/inbox"],
                            cwd=self.repo, capture_output=True, text=True).stdout
        return len([l for l in st.splitlines() if l.strip()])

    def test_eight_concurrent_captures_all_commit_and_none_are_lost(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            runs = [pool.submit(run_brain, "capture", f"concurrent unique-token-{i}",
                                "--commit", repo=self.repo) for i in range(8)]
            results = [r.result() for r in runs]
        for r in results:
            self.assertEqual(r.returncode, 0, "a concurrent capture failed:\n" + r.stdout)
        self.assertEqual(self._committed(), 8, "not every capture committed")
        self.assertEqual(self._uncommitted(), 0, "a capture was left uncommitted (unbacked)")

    def test_capture_does_not_sweep_unrelated_staged_work(self):
        wip = self.repo / "knowledge/reference/wip.md"
        wip.write_text("---\nid: wip\nkind: reference\ntitle: wip\ntopics: [brain]\n"
                       "aliases: [wip]\ncreated: 2026-07-24\nstatus: current\nreview_by: null\n"
                       "---\n\nunrelated work in progress\n", encoding="utf-8")
        subprocess.run(["git", "add", str(wip)], cwd=self.repo, check=True, capture_output=True)
        run_brain("capture", "innocent capture", "--commit", repo=self.repo)
        show = subprocess.run(["git", "show", "--stat", "--oneline", "HEAD"],
                             cwd=self.repo, capture_output=True, text=True).stdout
        self.assertNotIn("wip.md", show, "capture swept unrelated staged WIP into its commit")

    def test_capture_refuses_to_commit_on_a_consolidate_branch(self):
        subprocess.run(["git", "checkout", "-q", "-b", "consolidate/2026-07-24"],
                       cwd=self.repo, check=True, capture_output=True)
        out = run_brain("capture", "would strand on branch", "--commit", repo=self.repo)
        self.assertEqual(out.returncode, 1, "capture committed onto the consolidate branch")
        self.assertIn("consolidation", out.stdout)
        notes = list((self.repo / "knowledge/inbox").glob("*would-strand*.md"))
        self.assertTrue(notes, "the note was lost rather than kept on disk")


class ReadStalenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_inbox_read_is_marked_provisional(self):
        cap = run_brain("capture", "provisional flavor marker", repo=self.repo)
        path = Path(cap.stdout.strip().splitlines()[-1])
        out = run_brain("read", str(path), repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PROVISIONAL", out.stdout)

    def test_expired_review_by_is_surfaced_on_read(self):
        ref = self.repo / "knowledge/reference"
        (ref / "perishable.md").write_text(
            "---\nid: perishable\nkind: reference\ntitle: Perishable\ntopics: [brain]\n"
            "aliases: [perishable]\ncreated: 2020-01-01\nstatus: current\n"
            "review_by: 2020-06-01\n---\n\nA fact that should have been re-checked long ago.\n",
            encoding="utf-8")
        out = run_brain("read", "perishable", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("review_by", out.stdout)
        self.assertIn("STALE", out.stdout)


class LinkGraphTests(unittest.TestCase):
    """[[wikilinks]] were previously decorative: nothing validated them and
    nothing could answer 'what refers to this note?'. Supersede then left every
    hub page pointing at an archived note with no signal anywhere."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write(self, name, body, extra=""):
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {name}\ntopics: [brain]\n"
            f"aliases: [{name}]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"
            f"{extra}---\n\n{body}\n", encoding="utf-8")

    def links_json(self, note_id):
        out = run_brain("links", note_id, "--json", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json_payload(out.stdout)

    def test_backlinks_are_discovered(self):
        self.write("target-note", "The thing being referenced.")
        self.write("citing-a", "See [[target-note]] for detail.")
        self.write("citing-b", "Also relies on [[target-note|the target]].")
        graph = self.links_json("target-note")
        self.assertEqual(sorted(n["id"] for n in graph["inbound"]), ["citing-a", "citing-b"],
                         "backlinks missed a referring note")

    def test_outbound_links_are_recorded(self):
        self.write("hub", "Points at [[leaf-one]] and [[leaf-two]].")
        self.write("leaf-one", "one")
        self.write("leaf-two", "two")
        graph = self.links_json("hub")
        self.assertEqual(sorted(n["id"] for n in graph["outbound"]), ["leaf-one", "leaf-two"])

    def test_dangling_link_in_a_canonical_note_is_an_error(self):
        self.write("has-bad-link", "Refers to [[no-such-note-anywhere]].")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1, "a dangling wikilink did not block the commit")
        self.assertIn("dangling wikilink", out.stdout)

    def test_link_to_a_superseded_note_warns_with_the_successor(self):
        """The exact rot supersede used to create silently."""
        self.write("original-rule", "The original rule.")
        out = run_brain("supersede", "original-rule", "Original rule v2", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        # a hub still pointing at the OLD id
        self.write("stale-hub", "Follow [[original-rule]] for the rules.")
        lint = run_brain("lint", repo=self.repo)
        self.assertIn("points at a SUPERSEDED note", lint.stdout)
        self.assertIn("repoint it at", lint.stdout)

    def test_successor_may_cite_its_own_predecessor_without_warning(self):
        """`Supersedes [[old-id]]` is what the protocol itself writes — that one
        back-reference must never be reported as rot."""
        self.write("original-rule", "The original rule.")
        out = run_brain("supersede", "original-rule", "Original rule v2", repo=self.repo)
        self.assertEqual(out.returncode, 0, "setup: supersede failed\n" + out.stdout)
        lint = run_brain("lint", repo=self.repo)
        self.assertEqual(lint.returncode, 0, "a clean supersede left lint failing:\n" + lint.stdout)
        # Other notes citing the old id SHOULD warn — that is the feature. Only
        # the successor's own back-reference must be exempt.
        offending = [l for l in lint.stdout.splitlines()
                     if "points at a SUPERSEDED note" in l and "original-rule-v2.md" in l]
        self.assertEqual(offending, [],
                         "the successor was flagged for citing its own predecessor")

    def test_archived_notes_are_not_link_checked(self):
        """Archive is append-only history; a dangling link there would be an
        unfixable permanent lint error."""
        arch = self.repo / "knowledge/archive/reference"
        arch.mkdir(parents=True, exist_ok=True)
        self.write("successor", "current")
        (arch / "old.md").write_text(
            "---\nid: old-note\nkind: reference\ntitle: Old\ntopics: [brain]\n"
            "aliases: [old]\ncreated: 2026-06-01\nstatus: superseded\n"
            "superseded_by: successor\nreview_by: null\n---\n\n"
            "> SUPERSEDED 2026-07-01 by [[successor]] — x\n\nRefers to [[long-gone-note]].\n",
            encoding="utf-8")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0,
                         "a link inside archived history blocked the commit:\n" + out.stdout)

    def test_inbox_dangling_link_warns_but_does_not_block(self):
        (self.repo / "knowledge/inbox/thought.md").write_text(
            "---\ncreated: 2026-07-24\nstatus: draft\n---\n\nrelates to [[not-yet-written]]\n",
            encoding="utf-8")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0, "a provisional capture was blocked by a link check")
        self.assertIn("dangling wikilink", out.stdout)

    def test_read_surfaces_backlinks(self):
        self.write("popular", "body")
        self.write("refers-here", "cites [[popular]]")
        out = run_brain("read", "popular", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("linked from", out.stdout)
        self.assertIn("refers-here", out.stdout)

    def test_deleting_a_note_updates_the_graph(self):
        """The graph is derived — it must not outlive the notes it describes."""
        self.write("doomed", "body")
        self.write("pointer", "cites [[doomed]]")
        self.assertTrue(self.links_json("doomed")["inbound"])
        (self.ref / "pointer.md").unlink()
        self.assertEqual(self.links_json("doomed")["inbound"], [],
                         "graph still reports a backlink from a deleted note")


class SecretScannerTests(unittest.TestCase):
    """The built-in scanner is the LAST line of defence: SETUP documents a
    no-gitleaks path, so anything it misses can be committed and auto-pushed.

    A real paste of production-shaped env vars once sailed through it untouched
    (0 of 5 detected). Every sample below is assembled at runtime rather than
    written literally, so this test file never itself contains a secret-shaped
    string for gitleaks/CI to flag."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def samples(self):
        return {
            "Stripe secret key": "STRIPE_KEY=" + "sk_" + "live_" + "5tR1pE" * 5,
            "Google API key": "MAPS=" + "AIza" + "Bc9" * 13,
            "GitHub fine-grained PAT": "GH=" + "github_" + "pat_" + "aB3" * 12,
            "URL with embedded credentials":
                "DB=" + "postgres" + "://" + "admin" + ":" + "s3cretpassw0rd" + "@" + "db.x.io/y",
            "credential assignment": "aws_secret_" + "access_key=" + "wJalr" * 8,
        }

    def write_note(self, text):
        (self.repo / "knowledge" / "inbox" / "leak.md").write_text(
            f"---\ncreated: 2026-07-24\nstatus: draft\n---\n\n{text}\n", encoding="utf-8")

    def test_each_secret_class_is_detected(self):
        for label, sample in self.samples().items():
            with self.subTest(secret=label):
                self.write_note(sample)
                out = run_brain("lint", repo=self.repo)
                self.assertEqual(out.returncode, 1,
                                 f"{label} was NOT detected — it would commit and push:\n{sample}")
                self.assertIn(label, out.stdout, f"detected, but not reported as {label}")

    def test_a_clean_note_is_not_flagged(self):
        """Guard against the scanner becoming so eager it blocks ordinary prose."""
        self.write_note(
            "We agreed the password policy needs review, and the API key rotation\n"
            "is handled by the ops runbook. See the token lifetime discussion.\n"
            "Connection docs live at https://example.com/docs/postgres and nowhere else.\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0,
                         "ordinary prose about passwords/keys was flagged:\n" + out.stdout)

    def test_scanner_does_not_flag_its_own_pattern_definitions(self):
        """bin/brain scans every file including itself; permissive patterns must
        not match the source that defines them."""
        out = run_brain("lint", repo=self.repo)
        self.assertNotIn("bin/brain:", out.stdout.replace("bin/brain lint", ""),
                         "the scanner flagged its own source:\n" + out.stdout)


class CommitGateTests(unittest.TestCase):
    """The pre-commit gate is what protects the whole repo, and it had no
    end-to-end coverage: every prior test that touched it replaced it with a
    stub. These drive the REAL hook via a real `git commit`, so the scale work
    on `lint --staged` (which now sweeps only the staged paths) cannot quietly
    stop blocking things."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def commit(self, *paths):
        subprocess.run(["git", "add", *[str(p) for p in paths]], cwd=self.repo,
                       check=True, capture_output=True)
        return subprocess.run(["git", "commit", "-m", "probe"], cwd=self.repo,
                              capture_output=True, text=True)

    def note(self, rel, body, front=None):
        p = self.repo / "knowledge" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        head = front if front is not None else (
            "id: probe-note\nkind: reference\ntitle: Probe\ntopics: [brain]\n"
            "aliases: [probe]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n")
        p.write_text(f"---\n{head}---\n\n{body}\n", encoding="utf-8")
        return p

    def test_gate_blocks_a_secret_in_a_staged_note(self):
        p = self.repo / "knowledge/inbox/leak.md"
        p.write_text("---\ncreated: 2026-07-24\nstatus: draft\n---\n\nK="
                     + "sk_" + "live_" + "AbCdEfGhIjKlMnOpQrSt" + "\n", encoding="utf-8")
        self.assertNotEqual(self.commit(p).returncode, 0, "a secret was committed")

    def test_gate_blocks_malformed_frontmatter(self):
        p = self.repo / "knowledge/reference/bad.md"
        p.write_text("---\nBROKEN\n", encoding="utf-8")
        self.assertNotEqual(self.commit(p).returncode, 0)

    def test_gate_blocks_merge_conflict_markers(self):
        p = self.note("reference/conflict.md",
                      "## W\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n")
        self.assertNotEqual(self.commit(p).returncode, 0)

    def test_gate_blocks_a_dangling_wikilink(self):
        p = self.note("reference/dangle.md", "refers to [[no-such-note-at-all]]")
        self.assertNotEqual(self.commit(p).returncode, 0)

    def test_gate_blocks_a_duplicate_id_against_the_whole_tree(self):
        """A global invariant: catching it needs every OTHER note, not just the
        staged one — the staged-only sweep must not have cost us this."""
        first = self.note("reference/original.md", "x", front=(
            "id: collide-target\nkind: reference\ntitle: First\ntopics: [brain]\n"
            "aliases: [first]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        self.assertEqual(self.commit(first).returncode, 0, "setup note was rejected")
        p = self.note("reference/dup.md", "x", front=(
            "id: collide-target\nkind: reference\ntitle: Dup\ntopics: [brain]\n"
            "aliases: [dup]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        out = self.commit(p)
        self.assertNotEqual(out.returncode, 0, "a duplicate id was committed")
        self.assertIn("duplicate id", out.stdout + out.stderr)

    def test_gate_lets_a_valid_note_through(self):
        p = self.note("reference/fine.md", "## What\nA perfectly ordinary note.")
        out = self.commit(p)
        self.assertEqual(out.returncode, 0,
                         "the gate blocked a valid note:\n" + out.stdout + out.stderr)

    def test_gate_blocks_deleting_a_note_others_still_link_to(self):
        """The incremental gate only inspects what changed — so a deletion,
        whose own file is gone, could sail through while every note pointing at
        it silently rots. Caught by differentially testing the fast gate against
        the full one; this is that case nailed down."""
        target = self.note("reference/linked-to.md", "the referenced note", front=(
            "id: link-target\nkind: reference\ntitle: T\ntopics: [brain]\n"
            "aliases: [t]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        referrer = self.note("reference/refers.md", "see [[link-target]] for detail", front=(
            "id: referrer\nkind: reference\ntitle: R\ntopics: [brain]\n"
            "aliases: [r]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        self.assertEqual(self.commit(target, referrer).returncode, 0, "setup was rejected")
        subprocess.run(["git", "rm", "-q", "knowledge/reference/linked-to.md"],
                       cwd=self.repo, check=True, capture_output=True)
        out = subprocess.run(["git", "commit", "-m", "delete"], cwd=self.repo,
                             capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0,
                            "deleted a note that others still link to, breaking them silently")
        self.assertIn("dangling wikilink", out.stdout + out.stderr)

    def test_gate_allows_deleting_a_note_nothing_links_to(self):
        """The mirror image: the check must not make ordinary deletion painful."""
        lonely = self.note("reference/lonely.md", "nobody references this", front=(
            "id: lonely-note\nkind: reference\ntitle: L\ntopics: [brain]\n"
            "aliases: [l]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        self.assertEqual(self.commit(lonely).returncode, 0)
        subprocess.run(["git", "rm", "-q", "knowledge/reference/lonely.md"],
                       cwd=self.repo, check=True, capture_output=True)
        out = subprocess.run(["git", "commit", "-m", "delete"], cwd=self.repo,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0,
                         "blocked a harmless deletion:\n" + out.stdout + out.stderr)

    def test_gate_judges_the_staged_blob_not_the_worktree(self):
        """The whole point of --staged: staging a bad blob and then fixing the
        working copy must still be refused, or a secret reaches the remote."""
        p = self.repo / "knowledge/inbox/z.md"
        p.write_text("---\ncreated: 2026-07-24\nstatus: draft\n---\n\nK="
                     + "sk_" + "live_" + "ZzYyXxWwVvUuTtSsRrQq" + "\n", encoding="utf-8")
        subprocess.run(["git", "add", str(p)], cwd=self.repo, check=True, capture_output=True)
        p.write_text("---\ncreated: 2026-07-24\nstatus: draft\n---\n\nnow clean\n",
                     encoding="utf-8")            # fix the WORKTREE only
        out = subprocess.run(["git", "commit", "-m", "probe"], cwd=self.repo,
                             capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0,
                            "committed a bad STAGED blob because the worktree looked clean")


class LocalRemoteTests(unittest.TestCase):
    """`git clone ~/brain` points origin at the SOURCE repo, and post-commit
    auto-pushes after every commit — so a copy made to experiment in would push
    its throwaway branches straight back into the real brain. That actually
    happened during development. A path on this machine is not a backup and is
    never pushed to."""

    def setUp(self):
        self.tmp = temp_dir()
        self.source = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "s@example.com"], ["user.name", "Source"]):
            subprocess.run(["git", "config", *cfg], cwd=self.source, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.source, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "base"],
                       cwd=self.source, check=True, capture_output=True)
        self.clone = Path(self.tmp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.clone)],
                       check=True, capture_output=True)
        for cfg in (["user.email", "c@example.com"], ["user.name", "Clone"],
                    ["core.hooksPath", ".githooks"]):
            subprocess.run(["git", "config", *cfg], cwd=self.clone, check=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def source_branches(self):
        out = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                             cwd=self.source, capture_output=True, text=True).stdout
        return {b.strip() for b in out.splitlines() if b.strip()}

    def test_a_scratch_clone_never_pushes_back_into_the_source_brain(self):
        subprocess.run(["git", "checkout", "-q", "-b", "experiment"],
                       cwd=self.clone, check=True, capture_output=True)
        out = run_brain("capture", "throwaway experiment", "--commit", repo=self.clone)
        self.assertEqual(out.returncode, 0, "setup: the capture did not commit\n" + out.stdout)
        time.sleep(3)                      # post-commit is detached; let it try
        self.assertNotIn("experiment", self.source_branches(),
                         "a scratch clone pushed its branch into the source brain")
        log = subprocess.run(["git", "log", "--all", "--oneline"], cwd=self.source,
                             capture_output=True, text=True).stdout
        self.assertNotIn("throwaway", log, "clone commits leaked into the source brain")

    def test_doctor_refuses_to_call_a_local_path_a_backup(self):
        out = run_brain("doctor", repo=self.clone)
        self.assertIn("remote is a local path", out.stdout)
        self.assertNotEqual(out.returncode, 0,
                            "doctor reported healthy for a brain with no real backup")

    def test_local_remote_detection(self):
        """Mirrors the same test in .githooks/post-commit — keep them in step."""
        import importlib.util
        spec = importlib.util.spec_from_loader("brainmod", loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(BRAIN)
        exec(compile(BRAIN.read_text(encoding="utf-8"), "brain", "exec"), module.__dict__)
        is_local = module.__dict__["is_local_remote"]
        for url in ("https://github.com/u/r.git", "git@github.com:u/r.git",
                    "ssh://git@host/u/r.git", "git://host/r.git"):
            self.assertFalse(is_local(url), f"{url} is a real remote")
        for url in ("/Users/x/brain", "file:///Users/x/brain", "../other",
                    "./copy", "~/brain", "/tmp/gone"):
            self.assertTrue(is_local(url), f"{url} is a local path")


class TemplateTests(unittest.TestCase):
    """`brain template` is what makes this repo publishable. If it ever ships a
    note, a key, or machine-local wiring, private knowledge goes public — so
    the guarantees are asserted, not trusted."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.dest = Path(self.tmp.name) / "template"
        # Plant content that must NOT ship: a note, and a vault recipient.
        # Built at runtime: a literal canary here would also live in this test
        # file, which the template legitimately ships, and self-trip the check.
        self.canary = "CONFIDENTIAL" + "-" + "CANARY"
        (self.repo / "knowledge/reference/private-thing.md").write_text(
            "---\nid: private-thing\nkind: reference\ntitle: Private\ntopics: [brain]\n"
            "aliases: [private]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"
            f"---\n\nA {self.canary} fact about the owner.\n", encoding="utf-8")
        (self.repo / "setup/vault-recipient.txt").write_text(
            "age1exampleexamplerecipientkeyvalue\n", encoding="utf-8")

    def tearDown(self):
        cleanup_temp(self.tmp)

    def generate(self):
        out = run_brain("template", str(self.dest), repo=self.repo)
        self.assertEqual(out.returncode, 0, "template generation failed:\n" + out.stdout)
        return out

    # Deliberately hardcoded rather than read from TEMPLATE_KEEP_KNOWLEDGE:
    # widening what the template ships must require editing this list, or the
    # test would rubber-stamp whatever the code decided to publish.
    ALLOWED_IN_TEMPLATE = {"index.md", "vault/README.md", "reference/note-conventions.md"}

    def test_no_note_of_the_owners_ships(self):
        self.generate()
        shipped = {str(p.relative_to(self.dest / "knowledge"))
                   for p in (self.dest / "knowledge").rglob("*.md")}
        unexpected = shipped - self.ALLOWED_IN_TEMPLATE
        self.assertEqual(unexpected, set(), f"owner notes shipped: {sorted(unexpected)}")
        blob = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in self.dest.rglob("*") if p.is_file())
        self.assertNotIn(self.canary, blob, "note CONTENT leaked into the template")

    def test_vault_recipient_and_machine_local_wiring_do_not_ship(self):
        self.generate()
        self.assertFalse((self.dest / "setup/vault-recipient.txt").exists(),
                         "the author's age recipient shipped — strangers would "
                         "encrypt to a key they cannot read back")
        self.assertFalse((self.dest / ".mcp.json").exists())
        self.assertFalse((self.dest / "setup/skills/brain/SKILL.md").exists())

    def test_the_scaffolding_a_brain_needs_does_ship(self):
        self.generate()
        for required in ("bin/brain", "bin/brain-mcp", "CLAUDE.md", "SETUP.md",
                         "knowledge/index.md", "knowledge/topics.yaml",
                         ".githooks/pre-commit", "tests/test_brain.py"):
            self.assertTrue((self.dest / required).exists(), f"missing {required}")
        for folder in ("decisions", "reference", "topics", "people", "life",
                       "projects", "inbox", "archive", "vault"):
            self.assertTrue((self.dest / "knowledge" / folder).is_dir(),
                            f"knowledge/{folder}/ missing from the skeleton")

    def test_generated_template_is_lint_clean_and_usable(self):
        """An empty brain must accept its owner's very first note."""
        self.generate()
        self.assertEqual(run_brain("lint", repo=self.dest).returncode, 0)
        made = run_brain("new", "decision", "First real decision",
                         "--topics", "brain", repo=self.dest)
        self.assertEqual(made.returncode, 0, "a fresh brain rejected its first note:\n"
                         + made.stdout + made.stderr)
        self.assertEqual(run_brain("lint", repo=self.dest).returncode, 0)

    def test_refuses_to_overwrite_a_non_empty_destination(self):
        self.dest.mkdir(parents=True)
        (self.dest / "something-important.txt").write_text("do not lose me\n", encoding="utf-8")
        out = run_brain("template", str(self.dest), repo=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertTrue((self.dest / "something-important.txt").exists(),
                        "template generation clobbered an existing directory")


class DoctorExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_no_remote_makes_doctor_exit_nonzero(self):
        """doctor is the backup alarm: a repo with no remote is unbacked, so
        doctor must exit non-zero so the scheduled watchdog notifies. (A fresh
        sandbox has no origin.)"""
        out = run_brain("doctor", repo=self.repo)
        self.assertNotEqual(out.returncode, 0,
                            "an unbacked repo (no remote) reported healthy:\n" + out.stdout)
        self.assertIn("no git remote", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
