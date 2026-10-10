"""Stamps each ADR's Merged row with the date it reached main (docs/adr/README.md, Statuses).

Run by .github/workflows/adr-merged.yml on every push to main, never by hand. The date comes
from git, not from the push event: the committer date of the first-parent commit that added
the ADR, following renames, in the committer's own time zone, as `git log` shows it. With
squash merges that commit is the merge itself. Every pending ADR is checked on every run, so
a run that was missed or failed is put right by the next one.

Only `pending` is ever replaced. A date already there is kept even where git disagrees,
because git's history isn't always the real one: the public repository starts from one
squashed commit, which would otherwise restamp every ADR with the day of the cutover.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_NAME = re.compile(r"^\d{4}-.+\.md$")
# The value stops at the cell's closing bar; CRLF files keep their line endings.
MERGED_ROW = re.compile(r"^\| Merged \| (?P<value>[^|\r\n]*?) \|", re.M)


def merged_dates(repo: Path) -> dict[str, str]:
    """Each committed ADR's file name, mapped to the date it reached the checked-out branch.

    Walks the first-parent history of docs/adr oldest first: an add starts a date, a rename
    carries it to the new name, a delete drops it. Not `git log --follow`, which also takes
    a new file for a copy of a similar older one, and every ADR resembles the template.
    """
    out = subprocess.run(
        ["git", "log", "--first-parent", "--reverse", "-M",
         "--name-status", "--format=@%cs", "--", "docs/adr"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    dates: dict[str, str] = {}
    day = ""
    for line in out.splitlines():
        if line.startswith("@"):
            day = line[1:]
            continue
        fields = line.split("\t")
        status = fields[0][:1]
        if status == "A":
            dates[fields[1]] = day
        elif status == "R":
            dates[fields[2]] = dates.pop(fields[1], day)
        elif status == "D":
            dates.pop(fields[1], None)
    return {Path(path).name: day for path, day in dates.items()}


def stamp(repo: Path) -> list[str]:
    """Stamps every committed ADR whose Merged row is pending; returns the changed file names."""
    dates = merged_dates(repo)
    changed = []
    for path in sorted((repo / "docs" / "adr").iterdir()):
        if not ADR_NAME.match(path.name):
            continue
        text = path.read_bytes().decode("utf-8")
        row = MERGED_ROW.search(text)
        if row is None:
            raise ValueError(f"{path.name} has no Merged row")
        date = dates.get(path.name)
        if date is None or row["value"] != "pending":
            continue
        path.write_bytes((text[:row.start("value")] + date + text[row.end("value"):]).encode("utf-8"))
        changed.append(path.name)
    return changed


def main() -> int:
    for name in stamp(ROOT):
        print(f"stamped {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
