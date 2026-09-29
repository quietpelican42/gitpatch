"""Score gitpatch and the incumbent parsers against git on the corpus.

    python bench/incumbents.py CORPUS_DIR [repo]

Run it in an environment that has unidiff, whatthepatch, patch-ng and pygit2
installed (they are not dependencies of gitpatch). A file entry counts as right
when the parser reports the same paths (both sides for a rename or copy) and
the same added/removed line counts as ``git diff --raw -z`` / ``--numstat -z``
(``-`` for a binary file). A matching entry anywhere in the parser's list
counts. The "+status" column also compares the change type where the parser
reports one.
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
warnings.filterwarnings("ignore")

import corpus  # noqa: E402
import synthetic  # noqa: E402
from gitpatch import parse  # noqa: E402

# (status letter, old path, new path, added, deleted) as strings; None when not reported
Entry = tuple[str | None, str | None, str | None, str | None, str | None]


def _strip(p: str | None) -> str | None:
    if p is None or p == "/dev/null":
        return None
    p = p.split("\t")[0]
    return p[2:] if p[:2] in ("a/", "b/") else p


def run_gitpatch(text: bytes) -> list[Entry]:
    return [
        (
            {"added": "A", "deleted": "D", "renamed": "R", "copied": "C", "type-changed": "T", "modified": "M"}[f.status],
            f.old_path,
            f.new_path,
            "-" if f.is_binary else str(f.added),
            "-" if f.is_binary else str(f.deleted),
        )
        for f in parse(text).files
    ]


def run_unidiff(text: bytes) -> list[Entry]:
    import unidiff

    ps = unidiff.PatchSet(text.decode("utf-8", "surrogateescape"))
    out: list[Entry] = []
    for f in ps:
        st = "A" if f.is_added_file else "D" if f.is_removed_file else "R" if f.is_rename else "M"
        out.append((st, _strip(f.source_file), _strip(f.target_file), "-" if f.is_binary_file else str(f.added), "-" if f.is_binary_file else str(f.removed)))
    return out


def run_whatthepatch(text: bytes) -> list[Entry]:
    import whatthepatch

    out: list[Entry] = []
    for d in whatthepatch.parse_patch(text.decode("utf-8", "surrogateescape")):
        h = d.header
        ch = d.changes or []
        a = sum(1 for c in ch if c.old is None and c.new is not None)
        r = sum(1 for c in ch if c.new is None and c.old is not None)
        out.append((None, _strip(h.old_path) if h else None, _strip(h.new_path) if h else None, str(a), str(r)))
    return out


def run_patch_ng(text: bytes) -> list[Entry]:
    import patch_ng

    ps = patch_ng.fromstring(text)
    if not ps:
        return []
    out: list[Entry] = []
    for p in ps.items:
        a = sum(1 for h in p.hunks for ln in h.text if ln.startswith(b"+"))
        d = sum(1 for h in p.hunks for ln in h.text if ln.startswith(b"-"))
        out.append((None, _strip(p.source.decode("utf-8", "surrogateescape")), _strip(p.target.decode("utf-8", "surrogateescape")), str(a), str(d)))
    return out


def run_pygit2(text: bytes) -> list[Entry]:
    import pygit2

    diff = pygit2.Diff.parse_diff(text.decode("utf-8", "surrogateescape"))
    out: list[Entry] = []
    for p in diff:
        d = p.delta
        _, a, dl = p.line_stats
        b = d.is_binary
        out.append((d.status_char(), d.old_file.path, d.new_file.path, "-" if b else str(a), "-" if b else str(dl)))
    return out


PARSERS: dict[str, Callable[[bytes], list[Entry]]] = {
    "gitpatch": run_gitpatch,
    "unidiff 1.0.1": run_unidiff,
    "whatthepatch 1.0.7": run_whatthepatch,
    "patch-ng 1.19.1": run_patch_ng,
    "pygit2 1.20.1 (libgit2)": run_pygit2,
}


def category(letter: str, paths: tuple[str, ...], counts: tuple[str, str]) -> str:
    if counts == ("-", "-") and letter not in "RC":
        return "binary"
    if letter == "T":
        return "type change"
    odd = any(any(ord(ch) > 127 or ch in ' \t"\\\n' for ch in p) for p in paths)
    if letter in "RC":
        return "rename/copy, quoted or spaced name" if odd else "rename/copy"
    if odd:
        return "quoted or spaced name"
    if counts == ("0", "0"):
        return "header only (mode change, empty file)"
    return "plain text edit/add/delete"


def expected(root: Path, case: corpus.Case) -> list[tuple[Entry, bool, str]]:
    raws, nums = corpus.oracle(root, case)
    out: list[tuple[Entry, bool, str]] = []
    for r, n in zip(raws, nums):
        letter = r.status[0]
        old = r.paths[0] if letter in "RC" else (None if letter == "A" else r.paths[0])
        new = r.paths[-1] if letter != "D" else None
        pure = r.oh == r.nh  # blob unchanged: rename, copy or mode change; binary-ness is not in the patch
        out.append(((letter, old, new, n[0], n[1]), pure, category(letter, r.paths, (n[0], n[1]))))
    return out


def entry_ok(name: str, want: Entry, got: Entry, *, pure: bool, strict: bool = False) -> bool:
    ws, wo, wn, wa, wd = want
    gs, go, gn, ga, gd = got
    rename = ws in ("R", "C")
    if rename:
        if (go, gn) != (wo, wn):
            return False
    else:
        # one path is enough: the new path, or the old path for a deletion
        if (wn if wn is not None else wo) != (gn if wn is not None else go):
            return False
    if strict and gs is not None and ws != gs and not (gs in ("R", "M") and ws in ("C", "T") and name != "gitpatch"):
        return False
    if (ga, gd) != (wa, wd):
        if not (pure and (wa, wd) == ("-", "-") and (ga, gd) == ("0", "0")):
            return False
    return True


def main() -> int:
    root = Path(sys.argv[1])
    only = sys.argv[2] if len(sys.argv) > 2 else None
    cases: list[corpus.Case] = []
    tip = synthetic.build(root / "synthetic.git")
    for name, _url, sha, _depth in [*corpus.REPOS, ("synthetic", "", tip, 0)]:
        if only and name != only:
            continue
        cases += corpus.make_cases(root, name, sha)
    sets: dict[str, list[corpus.Case]] = {
        "git diff (-M/-C/-B, --binary, default prefixes)": [
            c for c in cases if c.kind in ("diff", "range") and not any(f.startswith(("--no-prefix", "--src-prefix")) for f in c.flags)
        ],
        "git format-patch": [c for c in cases if c.kind == "format-patch"],
    }
    for title, group in sets.items():
        score = {n: 0 for n in PARSERS}
        strict_score = {n: 0 for n in PARSERS}
        crashed = {n: 0 for n in PARSERS}
        right_patches = {n: 0 for n in PARSERS}
        by_cat: dict[str, dict[str, list[int]]] = {}
        total = 0
        npatches = 0
        for case in group:
            text = corpus.patch_text(root, case)
            if not text.strip() or len(text) > corpus.MAX_PATCH_BYTES or text.count(b"\ndiff --") > corpus.MAX_ENTRIES:
                continue
            want = expected(root, case)
            npatches += 1
            total += len(want)
            for name, fn in PARSERS.items():
                try:
                    got: list[Any] = fn(text)
                except Exception:  # noqa: BLE001
                    crashed[name] += 1
                    got = []
                ok = 0
                for w, pure, cat in want:
                    # lenient: any reported entry that matches counts, whatever its position
                    good = any(entry_ok(name, w, g, pure=pure) for g in got)
                    strict = good and any(entry_ok(name, w, g, pure=pure, strict=True) for g in got)
                    ok += good
                    strict_score[name] += strict
                    cell = by_cat.setdefault(cat, {}).setdefault(name, [0, 0])
                    cell[0] += strict
                    cell[1] += 1
                score[name] += ok
                right_patches[name] += ok == len(want)
        print(f"\n{title}: {npatches} patches, {total} file entries")
        print(f"{'parser':26} {'paths+counts':>16} {'+status':>16} {'patches right':>14} {'crashed':>8}")
        for name in PARSERS:
            print(f"{name:26} {score[name]:>6}/{total} {100 * score[name] / max(total, 1):5.1f}% {strict_score[name]:>6}/{total} {100 * strict_score[name] / max(total, 1):5.1f}% {right_patches[name]:>7}/{npatches} {crashed[name]:>8}")
        print("\nby entry kind (paths+counts+status):")
        names = ["gitpatch", "unidiff 1.0.1", "whatthepatch 1.0.7", "pygit2 1.20.1 (libgit2)"]
        print(f"{'kind':40} {'entries':>8} " + " ".join(f"{n.split()[0]:>12}" for n in names))
        for cat, cells in sorted(by_cat.items(), key=lambda kv: -kv[1]["gitpatch"][1]):
            n_e = cells["gitpatch"][1]
            print(f"{cat:40} {n_e:>8} " + " ".join(f"{100 * cells[n][0] / n_e:>11.1f}%" for n in names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
