"""ADR Opened and Merged rows (docs/adr/README.md, Statuses) and the script that stamps Merged.

The stamp runs on main (.github/workflows/adr-merged.yml); here it runs against throwaway git
repositories with fixed commit dates.
"""

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adr"
WORKFLOWS = ROOT / ".github" / "workflows"
sys.path.insert(0, str(ROOT / "scripts"))

import stamp_adr_merged  # noqa: E402

ISO_DATE = r"\d{4}-\d{2}-\d{2}"


def _adrs():
    return sorted(p for p in ADR_DIR.iterdir() if stamp_adr_merged.ADR_NAME.match(p.name))


def _row(text, field):
    m = re.search(rf"^\| {field} \| ([^|\r\n]*?) \|", text, re.M)
    return m and m[1]


def test_there_are_adrs():
    """Guards the checks below against passing on an empty or moved folder."""
    assert _adrs()


@pytest.mark.parametrize("path", _adrs(), ids=lambda p: p.name[:4])
def test_every_adr_has_an_opened_date_and_a_merged_date_or_pending(path):
    text = path.read_text(encoding="utf-8")
    opened, merged = _row(text, "Opened"), _row(text, "Merged")
    assert _row(text, "Date") is None
    assert opened and re.fullmatch(ISO_DATE, opened)
    assert merged == "pending" or (merged and re.fullmatch(ISO_DATE, merged))
    if merged != "pending":
        assert date.fromisoformat(merged) >= date.fromisoformat(opened)


def test_the_template_starts_merged_as_pending():
    text = (ADR_DIR / "template.md").read_text(encoding="utf-8")
    assert _row(text, "Merged") == "pending"
    assert _row(text, "Opened") == "YYYY-MM-DD"


def test_only_the_stamp_workflow_writes_and_it_runs_only_on_pushes_to_main():
    texts = {p.name: p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.y*ml"))}
    # Every workflow sets its token's permissions, so none falls back to the repo's default.
    assert [n for n, t in texts.items() if not re.search(r"^permissions:\s*$", t, re.M)] == []
    assert [n for n, t in texts.items() if re.search(r"\b(read|write)-all\b", t)] == []
    assert [n for n, t in texts.items() if re.search(r":\s*write\b", t)] == ["adr-merged.yml"]
    text = texts["adr-merged.yml"]
    # It writes only contents.
    assert re.findall(r"^\s*([\w-]+):\s*write\b", text, re.M) == ["contents"]
    trigger = text[text.index("\non:"):text.index("\npermissions:")]
    assert re.sub(r"\s+", " ", trigger).strip() == "on: push: branches: [main]"
    assert "python3 scripts/stamp_adr_merged.py" in text
    assert "git commit -q -m \"Docs: stamp ADR merge dates\" -- docs/adr" in text


# --- the script, against throwaway repositories ---

ADR = "# ADR-{n}: Example\n\n| Field | Value |\n| --- | --- |\n| Status | Accepted |\n| Opened | 2026-01-01 |\n| Merged | {merged} |\n"


class Repo:
    def __init__(self, path: Path):
        self.path = path
        (path / "docs" / "adr").mkdir(parents=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args, when="2026-01-01T12:00:00+0000"):
        env = {
            **os.environ,
            # Isolated from the developer's git config: no signing, hooks or templates.
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
        }
        return subprocess.run(["git", *args], cwd=self.path, env=env, check=True,
                              capture_output=True, text=True).stdout

    def write(self, name, text, newline="\n"):
        (self.path / "docs" / "adr" / name).write_bytes(text.replace("\n", newline).encode())

    def read(self, name):
        return (self.path / "docs" / "adr" / name).read_bytes().decode()

    def commit(self, when, *names):
        self.git("add", "--", *[f"docs/adr/{n}" for n in names])
        self.git("commit", "-q", "-m", "change", when=when)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_a_pending_adr_gets_its_commit_date_in_the_committers_own_time_zone(repo):
    repo.write("0001-a.md", ADR.format(n="0001", merged="pending"))
    # 23:30 at -07:00 is already the next day in UTC; the committer's day counts.
    repo.commit("2026-03-01T23:30:00-0700", "0001-a.md")
    assert stamp_adr_merged.stamp(repo.path) == ["0001-a.md"]
    assert "| Merged | 2026-03-01 |" in repo.read("0001-a.md")


def test_an_adr_from_a_merged_branch_gets_the_merge_date_not_the_branch_commits(repo):
    repo.write("0001-a.md", ADR.format(n="0001", merged="2026-01-01"))
    repo.commit("2026-01-01T12:00:00+0000", "0001-a.md")
    repo.git("switch", "-q", "-c", "feature")
    repo.write("0002-b.md", ADR.format(n="0002", merged="pending"))
    repo.commit("2026-02-01T12:00:00+0000", "0002-b.md")
    repo.git("switch", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "feature", when="2026-02-20T12:00:00+0000")
    assert stamp_adr_merged.stamp(repo.path) == ["0002-b.md"]
    assert "| Merged | 2026-02-20 |" in repo.read("0002-b.md")


def test_an_adr_renumbered_before_its_stamp_keeps_the_date_it_first_merged(repo):
    repo.write("0005-a.md", ADR.format(n="0005", merged="pending"))
    repo.commit("2026-01-10T12:00:00+0000", "0005-a.md")
    repo.git("mv", "docs/adr/0005-a.md", "docs/adr/0001-a.md")
    repo.git("commit", "-q", "-m", "renumber", when="2026-04-01T12:00:00+0000")
    assert stamp_adr_merged.stamp(repo.path) == ["0001-a.md"]
    assert "| Merged | 2026-01-10 |" in repo.read("0001-a.md")


def test_a_date_already_there_is_kept_even_after_a_squashed_history(repo):
    """The public repository starts from one squashed commit; its dates must survive it."""
    repo.write("0001-a.md", ADR.format(n="0001", merged="2026-01-06"))
    repo.write("0002-b.md", ADR.format(n="0002", merged="pending"))
    repo.commit("2026-11-01T12:00:00+0000", "0001-a.md", "0002-b.md")
    assert stamp_adr_merged.stamp(repo.path) == ["0002-b.md"]
    assert "| Merged | 2026-01-06 |" in repo.read("0001-a.md")
    assert "| Merged | 2026-11-01 |" in repo.read("0002-b.md")


def test_an_uncommitted_adr_stays_pending(repo):
    repo.write("0001-a.md", ADR.format(n="0001", merged="2026-01-05"))
    repo.commit("2026-01-05T12:00:00+0000", "0001-a.md")
    repo.write("0002-b.md", ADR.format(n="0002", merged="pending"))
    assert stamp_adr_merged.stamp(repo.path) == []
    assert "| Merged | pending |" in repo.read("0002-b.md")


def test_the_readme_and_template_are_never_stamped(repo):
    for name in ("README.md", "template.md"):
        repo.write(name, ADR.format(n="NNNN", merged="pending"))
    repo.commit("2026-01-05T12:00:00+0000", "README.md", "template.md")
    assert stamp_adr_merged.stamp(repo.path) == []
    assert "| Merged | pending |" in repo.read("template.md")


def test_crlf_line_endings_are_kept(repo):
    repo.write("0001-a.md", ADR.format(n="0001", merged="pending"), newline="\r\n")
    repo.commit("2026-01-05T12:00:00+0000", "0001-a.md")
    stamp_adr_merged.stamp(repo.path)
    text = repo.read("0001-a.md")
    assert "| Merged | 2026-01-05 |\r\n" in text
    assert "\n" not in text.replace("\r\n", "")


def test_an_adr_without_a_merged_row_fails_the_run(repo):
    repo.write("0001-a.md", "# ADR-0001\n\n| Opened | 2026-01-01 |\n")
    repo.commit("2026-01-05T12:00:00+0000", "0001-a.md")
    with pytest.raises(ValueError, match="0001-a.md"):
        stamp_adr_merged.stamp(repo.path)
