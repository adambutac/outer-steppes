"""Every action a workflow uses is pinned by full commit SHA (the header comment in .github/workflows/ci.yml).

A tag can be moved to other code by whoever controls the action's repository; a SHA can't.
The trailing tag comment is what Dependabot reads and updates alongside the SHA.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
# Matches block-style `uses:` lines, the only form the workflows use. Any other line that
# mentions `uses` as a key (flow style, a quoted key) is reported as unparsed, and a quoted,
# ./local or docker:// ref fails the pin check: either way it fails closed.
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
ANY_USES = re.compile(r"""\buses["']?\s*:""")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
TAG_COMMENT = re.compile(r"^\s+#\s*v\d+(\.\d+)*\s*$")


def _uses_lines():
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = USES.match(line)
            if m:
                found.append((f"{path.name}:{n}", m["ref"], m["rest"]))
            elif ANY_USES.search(line.split("#", 1)[0]):
                found.append((f"{path.name}:{n}", "<unparsed>", line))
    return found


def test_the_workflows_use_actions():
    """Guards the check below against passing on an empty or moved workflows folder."""
    assert _uses_lines()


def test_every_action_is_pinned_by_commit_sha_with_its_tag_in_a_comment():
    bad = [(where, ref + rest) for where, ref, rest in _uses_lines()
           if not PINNED.match(ref) or not TAG_COMMENT.match(rest)]
    assert bad == []
