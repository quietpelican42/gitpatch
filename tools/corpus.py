"""Build and check the real-repository corpus.

    python tools/corpus.py fetch DIR        # clone the pinned commits (needs network)
    python tools/corpus.py check DIR        # run every case against git

The corpus is not stored in this repository. ``fetch`` downloads the pinned
commits of the repositories listed in ``REPOS``; ``check`` generates patches
with the installed git and compares gitpatch with git's own ``--raw -z`` and
``--numstat -z`` output for every file entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthetic  # noqa: E402
from gitpatch import ApplyError, FileChange, Patch, PatchError, parse  # noqa: E402

# (name, url, pinned commit, history depth fetched; 0 is the full history)
REPOS: list[tuple[str, str, str, int]] = [
    ("cpython", "https://github.com/python/cpython.git", "aa287d7f443c2e219b43f7b804b209971964d206", 300),
    ("git", "https://github.com/git/git.git", "a018953688f1b10bddf91bff8747068f5f4746a4", 0),
    ("django", "https://github.com/django/django.git", "9332b163a67eabe5bdbf8066b1c5da394be9aa9c", 300),
    ("vim", "https://github.com/vim/vim.git", "3e270064ab469ab44224a45dad4c84497acbfde5", 300),
    ("libgit2", "https://github.com/libgit2/libgit2.git", "0551dfd4ad989b6a3d5683c0d4cf326c6efef929", 0),
    ("pdf.js", "https://github.com/mozilla/pdf.js.git", "c5d4809f112295f9ae3150566f50f00385c8a665", 1500),
]

# repositories where only specific commits are fetched (each with one parent): commits that
# touch paths containing non-ASCII characters
COMMIT_LISTS: dict[str, list[str]] = {
    "php-src": [
        "02fc2fe8c91130035e4815059925781eea2d2554",
        "08d8623dec87c002fa7db582ef6feceeb6c4be8b",
        "0e38909170b92ae3da5efd18b7b92df52c80051d",
        "128cd0d0f2dd07c5331d609f1906c5e4fe3e9f98",
        "1ad08256f349fa513157437abc4feb245cce03fc",
        "29cb3c89e50376f254e6a63cf76ac96dd309b366",
        "2a062f3c020e8cc53e49fa823021f3e8ce591cfe",
        "2d48d734a20192a10792669baaa88dbe86f2b3a6",
        "31a3956053f9a6f8878ba471768c897f20ee45aa",
        "34fa65a6c2840e03f4850bb10de0579a20849cb3",
        "37c329d7153b65fd2430fa42e8af8e8a0cbc0a41",
        "3b9173dc8f3bda99e87003f6b6b2a16fa25c1b72",
        "3d3f11ede4cc7c83d64cc5edaae7c29ce9c6986f",
        "411e97c72d6a200930a7edc48a031a2ca64c0e0c",
        "4f793ba1c323210171574bfc94d2ac2b81a666f2",
        "5bd18e3fdc1abdedd5c418095fd8a41f77bae146",
        "6219d7fbc66d8d254f5d795b1aeaa1d1dbddd2b5",
        "622b10f06e5f9ba080b748b7b5b1807b142f27fd",
        "a24727a5ca2b9a3949f98689ee6b8a2fb867294f",
        "a68e0767b349360344fcdfa0f853bf577c37adbd",
        "afdaa91170a1f7b0387cf577d957d4d6c015c136",
        "bd24c562075de9cb5e3d866d54270a27eca400a7",
        "bf93f1d824518de3764ecab1265514c6e96f88e8",
        "c3eeab01bac39234bbe09407f4bddf8f22b6af53",
        "d874d4a15c2e17a753d580691906c95e9153b823",
        "e2182a1ba7cdd3c915cf29cd8367a6e02a0c10c8",
    ],
}
REPOS.append(("php-src", "https://github.com/php/php-src.git", "", 2))

ENV = {
    **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")},
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "TZ": "UTC",
    "LC_ALL": "C",
}
MAX_PATCH_BYTES = 3_000_000
MAX_ENTRIES = 250
TIMEOUT = 120


def git(repo: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=ENV,
        timeout=TIMEOUT,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')}")
    return proc.stdout


def fetch(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, url, sha, depth in REPOS:
        repo = root / f"{name}.git"
        if not repo.exists():
            subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True, env=ENV, timeout=60)
        if name in COMMIT_LISTS:
            for c in COMMIT_LISTS[name]:
                if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", c], env=ENV, timeout=60).returncode != 0:
                    print(f"fetching {name} @ {c[:12]} depth 2", flush=True)
                    for attempt in range(3):  # GitHub occasionally refuses a fetch by id; retry
                        done = subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--no-tags", "--depth=2", url, c], env=ENV, timeout=600, capture_output=True)
                        if done.returncode == 0:
                            break
                    else:
                        print(f"warning: could not fetch {c}", file=sys.stderr)
            continue
        have = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", sha], env=ENV, timeout=60
        ).returncode
        if have != 0:
            print(f"fetching {name} @ {sha[:12]} depth {depth}", flush=True)
            subprocess.run(
                ["git", "-C", str(repo), "fetch", "-q", "--no-tags", *([f"--depth={depth}"] if depth else []), url, sha],
                check=True,
                env=ENV,
                timeout=1800,
            )


# -- case generation ---------------------------------------------------------

VARIANTS: list[list[str]] = [
    ["-M", "-C", "--binary"],
    ["-M", "-C", "--binary"],
    ["-M", "-C", "--binary"],
    ["-M", "--binary", "--no-prefix"],
    ["-M", "-C", "--binary", "--src-prefix=old/", "--dst-prefix=new/"],
    ["-M", "-C"],
    ["-M", "-C", "--binary", "-U0"],
    ["-B", "-M", "--binary"],
    ["-M", "-C", "--full-index", "-U10", "--patience"],
    ["--no-renames", "--binary"],
    ["-M", "-C", "--find-copies-harder", "--binary"],
    ["-M", "-C", "--binary", "--src-prefix=x/y/", "--dst-prefix=z/y/"],
]


@dataclass
class Case:
    repo: str
    kind: str  # diff | range | format-patch | log | show
    commits: list[str]  # commits whose (parent..commit) diffs make up the patch
    flags: list[str]
    label: str = ""
    args: list[str] = field(default_factory=list)


def _scan(root: Path, name: str, sha: str) -> dict[str, Counter[str]]:
    """Feature counts per non-merge commit, from two history scans (cached in ``root``)."""
    cache = root / f"features-{name}-{sha[:12]}.json"
    if cache.exists():
        return {k: Counter(v) for k, v in json.loads(cache.read_text()).items()}
    repo = root / f"{name}.git"
    feats: dict[str, Counter[str]] = {}
    raw = git(repo, "log", "--no-merges", "-M", "-C", "--raw", "-z", "--format=%x01%H", sha)
    for chunk in raw.split(b"\x01")[1:]:
        toks = chunk.split(b"\0")
        c = toks[0].decode()
        f: Counter[str] = Counter()
        i = 1
        while i < len(toks):
            if not toks[i].startswith(b":"):
                i += 1
                continue
            meta = toks[i].split()
            status = meta[4].decode()
            npaths = 2 if status[0] in "RC" else 1
            paths = toks[i + 1 : i + 1 + npaths]
            i += 1 + npaths
            f[{"R": "rename", "C": "copy", "T": "type", "D": "delete", "A": "add"}.get(status[0], "modify")] += 1
            if meta[0] != b":000000" and meta[1] != b"000000" and meta[0] != b":" + meta[1]:
                f["mode"] += 1
            if any(any(b > 127 for b in p) for p in paths):
                f["nonascii"] += 1
            if any(any(ch in p for ch in (b" ", b"\t", b'"', b"\\", b"\n")) for p in paths):
                f["oddname"] += 1
        feats[c] = f
    num = git(repo, "log", "--no-merges", "-M", "--numstat", "--format=%x01%H", sha)
    for chunk in num.split(b"\x01")[1:]:
        c = chunk[:40].decode()
        if c in feats and b"\n-\t-\t" in chunk:
            feats[c]["binary"] += 1
    cache.write_text(json.dumps({k: dict(v) for k, v in feats.items()}))
    return feats


def make_cases(root: Path, name: str, sha: str, per_feature: int = 6, plain: int = 14) -> list[Case]:
    repo = root / f"{name}.git"
    rng = random.Random(f"{name}:{sha}")
    if name == "synthetic":
        per_feature, plain = 40, 40
    cases: list[Case] = []
    if name in COMMIT_LISTS:
        for c in COMMIT_LISTS[name]:
            if git(repo, "cat-file", "-t", c, check=False).strip() != b"commit" or not git(repo, "rev-list", "--parents", "-n1", c).split()[1:]:
                continue
            for extra in ([["-M", "-C", "--binary"], ["-M", "--binary", "--no-prefix"], ["-M", "-C"]]):
                cases.append(Case(name, "diff", [c], extra, f"{c[:10]} diff {' '.join(extra)}"))
            cases.append(Case(name, "format-patch", [c], ["-M", "-C", "--binary"], f"{c[:10]} format-patch"))
            cases.append(Case(name, "show", [c], ["-M", "-C", "--binary"], f"{c[:10]} show"))
        return cases
    revs = git(repo, "rev-list", "--parents", sha).decode().split("\n")
    singles: list[str] = []
    merges: list[str] = []
    for row in revs:
        parts = row.split()
        if len(parts) == 2:
            singles.append(parts[0])
        elif len(parts) > 2 and len(merges) < 60:
            merges.append(parts[0])
    feats = _scan(root, name, sha)
    singles = [c for c in singles if c in feats]
    chosen: list[str] = []
    for feat in ("rename", "copy", "type", "mode", "nonascii", "oddname", "binary", "delete", "add"):
        pool = [c for c in singles if feats[c][feat] and c not in chosen]
        rng.shuffle(pool)
        pool.sort(key=lambda c: -sum(feats[c].values()) if rng.random() < 0.5 else 0)
        chosen += pool[: {"nonascii": 25, "oddname": 12, "type": 10, "copy": 8, "mode": 8}.get(feat, per_feature)]
    rest = [c for c in singles[:300] if c not in chosen]
    rng.shuffle(rest)
    chosen += rest[:plain]
    for idx, c in enumerate(chosen):
        flags = VARIANTS[rng.randrange(len(VARIANTS))]
        for extra in VARIANTS if name == "synthetic" else [flags]:
            cases.append(Case(name, "diff", [c], extra, f"{c[:10]} diff {' '.join(extra)}"))
        cases.append(Case(name, "format-patch", [c], ["-M", "-C", "--binary"], f"{c[:10]} format-patch"))
        if idx % 3 == 0:
            cases.append(Case(name, "show", [c], ["-M", "-C", "--binary"], f"{c[:10]} show"))
    conflicted = [
        c for c in merges if git(repo, "diff-tree", "--cc", "-r", "--name-only", "--no-commit-id", c, check=False).strip()
    ]
    for c in conflicted[:5]:
        cases.append(Case(name, "show-cc", [c], ["--cc"], f"{c[:10]} show --cc"))
        cases.append(Case(name, "show-cc", [c], ["-c"], f"{c[:10]} show -c"))
    for c in merges[:3]:
        cases.append(Case(name, "show-m", [c], ["-m", "-M", "-C", "--binary"], f"{c[:10]} show -m"))
    # multi-commit ranges: one diff over c~k..c, and a log / format-patch series
    for k, idx in enumerate(range(0, min(len(singles), 60), 5)):  # ranges over recent history
        c = singles[idx]
        span = 2 + k % 5
        cases.append(Case(name, "range", [c], VARIANTS[rng.randrange(len(VARIANTS))], f"{c[:10]}~{span} range", [f"{c}~{span}", c]))
        series = [x for x in git(repo, "rev-list", "--no-merges", "--reverse", f"{c}~{span}..{c}").decode().split()]
        if series:
            cases.append(Case(name, "log", series, ["-M", "-C", "--binary"], f"{c[:10]}~{span} log", [f"{c}~{span}..{c}"]))
            cases.append(Case(name, "format-patch", series, ["-M", "-C", "--binary"], f"{c[:10]}~{span} series", [f"{c}~{span}..{c}"]))
    return cases


def patch_text(root: Path, case: Case) -> bytes:
    repo = root / f"{case.repo}.git"
    if case.kind == "diff":
        c = case.commits[0]
        return git(repo, "diff", *case.flags, f"{c}~", c)
    if case.kind == "range":
        return git(repo, "diff", *case.flags, *case.args)
    if case.kind in ("show", "show-cc", "show-m"):
        return git(repo, "show", *case.flags, case.commits[0])
    if case.kind == "log":
        return git(repo, "log", "-p", "--no-merges", "--reverse", *case.flags, *case.args)
    if case.kind == "format-patch":
        if case.args:
            return git(repo, "format-patch", "--stdout", "--no-merges", *case.flags, *case.args)
        return git(repo, "format-patch", "--stdout", "-1", *case.flags, case.commits[0])
    raise ValueError(case.kind)


# -- oracle ------------------------------------------------------------------


@dataclass
class RawEntry:
    om: str
    nm: str
    oh: str
    nh: str
    status: str
    paths: tuple[str, ...]


def _strip_prefix_flags(flags: list[str]) -> list[str]:
    return [f for f in flags if not f.startswith(("--src-prefix", "--dst-prefix", "--no-prefix", "-U", "--full-index", "--binary"))]


def oracle(root: Path, case: Case) -> tuple[list[RawEntry], list[tuple[str, str, tuple[str, ...]]]]:
    repo = root / f"{case.repo}.git"
    base = _strip_prefix_flags(case.flags)
    raws: list[RawEntry] = []
    nums: list[tuple[str, str, tuple[str, ...]]] = []
    if case.kind == "range":
        targets = [("diff", case.args)]
    elif case.kind == "show-m":
        c = case.commits[0]
        targets = [("diff", [f"{c}^{k}", c]) for k in (1, 2)]
    elif case.kind == "diff" or case.kind == "show":
        targets = [("diff", [f"{case.commits[0]}~", case.commits[0]])]
    else:
        targets = [("diff", [f"{c}~", c]) for c in case.commits]
    for _, rev in targets:
        rw = git(repo, "diff", *base, "--raw", "-z", "--no-abbrev", *rev).split(b"\0")
        i = 0
        while i < len(rw) and rw[i].startswith(b":"):
            m = rw[i].decode().split()
            npaths = 2 if m[4][0] in "RC" else 1
            paths = tuple(p.decode("utf-8", "surrogateescape") for p in rw[i + 1 : i + 1 + npaths])
            raws.append(RawEntry(m[0][1:], m[1], m[2], m[3], m[4], paths))
            i += 1 + npaths
        ns = git(repo, "diff", *base, "--numstat", "-z", *rev).split(b"\0")
        i = 0
        while i < len(ns) - 1:
            a, d, rest = ns[i].decode("utf-8", "surrogateescape").split("\t", 2)
            if rest == "":
                nums.append((a, d, (ns[i + 1].decode("utf-8", "surrogateescape"), ns[i + 2].decode("utf-8", "surrogateescape"))))
                i += 3
            else:
                nums.append((a, d, (rest,)))
                i += 1
    return raws, nums


def _letter(f: FileChange) -> str:
    return {"added": "A", "deleted": "D", "modified": "M", "renamed": "R", "copied": "C", "type-changed": "T"}[f.status]


@dataclass
class Result:
    patches: int = 0
    entries: int = 0
    exact: int = 0
    gap: int = 0
    bad: list[str] = field(default_factory=list)
    binary_checked: int = 0
    binary_ok: int = 0
    apply_checked: int = 0
    apply_ok: int = 0
    combined: int = 0
    combined_ok: int = 0
    mail_checked: int = 0
    mail_ok: int = 0
    kinds: Counter[str] = field(default_factory=Counter)
    seen: Counter[str] = field(default_factory=Counter)


def compare_entry(f: FileChange, r: RawEntry, n: tuple[str, str, tuple[str, ...]]) -> tuple[str, str]:
    """Return ("exact"|"gap"|"bad", detail)."""
    problems: list[str] = []
    gap = False
    letter = r.status[0]
    if _letter(f) != letter:
        problems.append(f"status {_letter(f)} != {letter}")
    if letter in "RC":
        score = int(r.status[1:])
        if f.similarity != score:
            problems.append(f"similarity {f.similarity} != {score}")
    elif letter == "T" and len(r.status) > 1 and f.dissimilarity is None:
        gap = True  # -B prints a score for type changes; the patch does not carry it
    elif len(r.status) > 1:
        if f.dissimilarity != int(r.status[1:]):
            problems.append(f"dissimilarity {f.dissimilarity} != {r.status[1:]}")
    want_paths = r.paths
    have_paths = (f.old_path, f.new_path) if letter in "RC" else (f.path,)
    if have_paths != want_paths:
        problems.append(f"paths {have_paths!r} != {want_paths!r}")
    # modes
    om = "000000" if f.status == "added" else ("%06o" % f.old_mode if f.old_mode is not None else None)
    nm = "000000" if f.status == "deleted" else ("%06o" % f.new_mode if f.new_mode is not None else None)
    for have, want, tag in ((om, r.om, "old mode"), (nm, r.nm, "new mode")):
        if have is None:
            if f.status in ("renamed", "copied") and not f.hunks and not f.is_binary or f.status == "renamed":
                gap = True  # pure rename/copy: the patch has no mode information
            else:
                problems.append(f"{tag} missing (want {want})")
        elif have != want:
            problems.append(f"{tag} {have} != {want}")
    # hashes (patch carries abbreviations, or nothing)
    for have_h, want_h, tag in ((f.old_hash, r.oh, "old hash"), (f.new_hash, r.nh, "new hash")):
        zero = set(want_h) == {"0"}
        if have_h is None:
            if zero:
                continue
            if f.old_hash is None and f.new_hash is None:
                gap = True  # no index line in the patch
            else:
                problems.append(f"{tag} missing")
        elif zero or not want_h.startswith(have_h):
            problems.append(f"{tag} {have_h} vs {want_h}")
    # numstat
    a, d, npaths = n
    want_counts = (a, d)
    have_counts = ("-", "-") if f.is_binary else (str(f.added), str(f.deleted))
    if have_counts != want_counts:
        if (
            want_counts == ("-", "-")
            and not f.hunks
            and not f.is_binary
            and f.old_hash is None
            and f.new_hash is None
        ):
            gap = True  # pure rename or mode change of a binary file: the patch does not say it is binary
        else:
            problems.append(f"numstat {have_counts} != {want_counts}")
    if npaths != (want_paths if letter in "RC" else (f.path,)):
        problems.append(f"numstat paths {npaths!r}")
    if problems:
        return "bad", "; ".join(problems)
    return ("gap" if gap else "exact"), ""


def check_text_outputs(patch: Patch, raws: list[RawEntry], nums: list[tuple[str, str, tuple[str, ...]]]) -> list[str]:
    """Where the patch carries everything, raw(z=True)/numstat(z=True) must equal git's bytes."""
    problems: list[str] = []
    raw_out = patch.raw(z=True).split("\0")
    num_out = patch.numstat(z=True).split("\0")
    ri = 0
    ni = 0
    for f, r, n in zip(patch.files, raws, nums):
        if f.is_combined:
            continue
        npaths = 2 if r.status[0] in "RC" else 1
        mine_raw = (raw_out[ri], *raw_out[ri + 1 : ri + 1 + npaths])
        ri += 1 + npaths
        want_hdr = f":{r.om} {r.nm} "
        if not mine_raw[0].startswith(want_hdr) and f.old_mode is not None and f.new_mode is not None:
            problems.append(f"raw header {mine_raw[0]!r} vs {want_hdr!r}")
        if mine_raw[0].split(" ")[-1] != r.status and not (r.status[0] == "T" and mine_raw[0].split(" ")[-1] == "T"):
            problems.append(f"raw status {mine_raw[0]!r} vs {r.status}")
        if npaths == 2:
            first = num_out[ni]
            second = num_out[ni + 1 : ni + 3]
            ni += 3
            if second != [n[2][0], n[2][1]]:
                problems.append(f"numstat -z rename paths {second!r}")
            if first.split("\t")[:2] != [n[0], n[1]] and not (n[0] == "-" and not f.is_binary):
                problems.append(f"numstat -z counts {first!r}")
        else:
            first = num_out[ni]
            ni += 1
            no_content_info = not f.hunks and not f.is_binary and f.old_hash is None and f.new_hash is None
            if no_content_info and n[0] == "-":
                first = first.replace("0\t0\t", "-\t-\t", 1)  # binary-ness of a mode-only change is not in the patch
            if first != f"{n[0]}\t{n[1]}\t{n[2][0]}":
                problems.append(f"numstat -z {first!r} vs {n!r}")
    return problems


def check_binary(root: Path, repo: str, patch: Patch, res: Result) -> list[str]:
    problems: list[str] = []
    rp = root / f"{repo}.git"

    def blob(h: str | None) -> bytes:
        if h is None:
            return b""
        return git(rp, "cat-file", "blob", h)

    def full(h: str | None) -> str | None:
        if h is None:
            return None
        return git(rp, "rev-parse", h).decode().strip()

    for f in patch.files:
        for part in f.parts or (f,):
            if part.binary is None:
                continue
            res.binary_checked += 1
            old = blob(full(part.old_hash))
            new = blob(full(part.new_hash))
            try:
                got_new = part.binary.forward.apply(old)
                ok = got_new == new
                if part.binary.reverse is not None:
                    ok = ok and part.binary.reverse.apply(new) == old
                if part.apply(old if part.status != "added" else None) != (new if part.status != "deleted" else None):
                    ok = False
            except ApplyError as exc:
                ok = False
                problems.append(f"{part.path}: {exc}")
            if ok:
                res.binary_ok += 1
            else:
                problems.append(f"binary payload mismatch for {part.path}")
    return problems


def apply_flags(flags: list[str]) -> list[str]:
    """`git apply` options that match the prefix and context options used to make a patch."""
    out: list[str] = []
    if "-U0" in flags:
        out.append("--unidiff-zero")
    if "--no-prefix" in flags:
        out.append("-p0")
    for f in flags:
        if f.startswith("--src-prefix="):
            out.append("-p%d" % f.split("=", 1)[1].count("/"))
    return out


def prefix_overrides(flags: list[str]) -> tuple[str | None, str | None]:
    """Explicit prefixes for the multi-component prefix variant (not guessable from a header)."""
    src = dst = None
    for f in flags:
        if f.startswith("--src-prefix=") and f.count("/") > 1:
            src = f.split("=", 1)[1]
        if f.startswith("--dst-prefix=") and f.count("/") > 1:
            dst = f.split("=", 1)[1]
    return src, dst


def check_apply(root: Path, repo: str, text: bytes, patch: Patch, res: Result, rng: random.Random, flags: list[str]) -> list[str]:
    """Apply text entries and cross-check with `git apply` on a sample."""
    problems: list[str] = []
    rp = root / f"{repo}.git"
    lines = text.split(b"\n")
    for f in patch.files:
        if f.is_combined or f.is_binary or f.parts or 0o120000 in ((f.old_mode or 0) & 0o170000, (f.new_mode or 0) & 0o170000):
            continue
        if f.status not in ("modified", "renamed", "copied", "added", "deleted") or not f.hunks:
            continue
        if f.old_hash is None and f.status != "added":
            continue
        try:
            old = None if f.status == "added" else git(rp, "cat-file", "blob", git(rp, "rev-parse", f.old_hash or "").decode().strip())
        except RuntimeError:
            continue
        new_full = git(rp, "rev-parse", f.new_hash).decode().strip() if f.new_hash else None
        new = git(rp, "cat-file", "blob", new_full) if new_full else None
        res.apply_checked += 1
        try:
            mine = f.apply(old)
        except ApplyError as exc:
            problems.append(f"apply {f.path}: {exc}")
            continue
        if mine != new:
            problems.append(f"apply {f.path}: result differs from blob")
            continue
        if rng.random() < 0.35 and f.status in ("modified", "renamed", "copied") and "-p2" not in apply_flags(flags):
            section = b"\n".join(lines[f.line - 1 : f.end_line]) + b"\n"
            with tempfile.TemporaryDirectory() as td:
                src = f.old_path or ""
                target = Path(td) / src.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    assert old is not None
                    target.write_bytes(old)
                except (OSError, UnicodeEncodeError):
                    res.apply_ok += 1
                    continue
                proc = subprocess.run(["git", "apply", *apply_flags(flags), "-"], input=section, cwd=td, env=ENV, capture_output=True, timeout=60)
                if proc.returncode != 0:
                    problems.append(f"git apply rejected {f.path}: {proc.stderr.decode(errors='replace')[:200]}")
                    continue
                out = Path(td) / (f.new_path or "")
                if not out.exists() or out.read_bytes() != mine:
                    problems.append(f"git apply result differs from gitpatch for {f.path}")
                    continue
        res.apply_ok += 1
    return problems


def check_mail(root: Path, case: Case, patch: Patch, res: Result) -> list[str]:
    if case.kind != "format-patch":
        return []
    rp = root / f"{case.repo}.git"
    problems: list[str] = []
    if len(patch.commits) != len(case.commits):
        return [f"{len(patch.commits)} mails for {len(case.commits)} commits"]
    for info, c in zip(patch.commits, case.commits):
        res.mail_checked += 1
        fmt = git(rp, "log", "-1", "--format=%an <%ae>%x00%s%x00%at", c).decode("utf-8", "surrogateescape").rstrip("\n")
        author, subject, at = fmt.split("\0")
        dt = info.date_parsed
        ok = (
            info.sha == c
            and info.author == author
            and info.title == subject
            and dt is not None
            and int(dt.timestamp()) == int(at)
        )
        if ok:
            res.mail_ok += 1
        else:
            problems.append(f"mail {c[:10]}: {info.author!r} {info.title!r} {info.date!r} vs {fmt!r}")
    return problems


def check_combined(root: Path, case: Case, patch: Patch, res: Result) -> list[str]:
    if case.kind != "show-cc":
        return []
    rp = root / f"{case.repo}.git"
    c = case.commits[0]
    names = {p for p in git(rp, "diff-tree", case.flags[0], "-r", "--name-only", "-z", "--no-commit-id", c).split(b"\0") if p}
    headers = [ln for ln in patch_text(root, case).split(b"\n") if ln.startswith((b"diff --cc ", b"diff --combined "))]
    got = [f.path.encode("utf-8", "surrogateescape") for f in patch.files if f.is_combined]
    want = got if len(headers) == len(got) and set(got) <= names else [b"<mismatch>"]
    res.combined += 1
    if got == want and all(f.is_combined for f in patch.files):
        res.combined_ok += 1
        return []
    return [f"combined: {got!r} vs {want!r}"]


def run_case(root: Path, case: Case, res: Result, rng: random.Random) -> None:
    text = patch_text(root, case)
    if len(text) > MAX_PATCH_BYTES or not text.strip():
        return
    if text.count(b"\ndiff --") > MAX_ENTRIES:
        return
    res.patches += 1
    res.kinds[case.kind] += 1
    label = f"{case.repo} {case.label}"
    try:
        src_prefix, dst_prefix = prefix_overrides(case.flags)
        patch = parse(text, src_prefix=src_prefix, dst_prefix=dst_prefix)
    except PatchError as exc:
        res.bad.append(f"{label}: PatchError {exc}")
        return
    if case.kind == "show-cc":
        res.seen["combined"] += len(patch.files)
        for msg in check_combined(root, case, patch, res):
            res.bad.append(f"{label}: {msg}")
        res.entries += len(patch.files)
        res.exact += len(patch.files)
        return
    raws, nums = oracle(root, case)
    if len(patch.files) != len(raws) or len(raws) != len(nums):
        res.bad.append(f"{label}: {len(patch.files)} entries vs raw {len(raws)} numstat {len(nums)}")
        res.entries += len(raws)
        return
    for f, r, n in zip(patch.files, raws, nums):
        res.entries += 1
        if f.is_combined:
            res.seen["combined"] += 1
        verdict, detail = compare_entry(f, r, n)
        if verdict == "bad":
            res.bad.append(f"{label}: {f.path}: {detail}")
        elif verdict == "gap":
            res.gap += 1
        else:
            res.exact += 1
        res.seen[f.status] += 1
        if f.is_binary:
            res.seen["binary"] += 1
        if f.old_mode is not None and f.new_mode is not None and f.old_mode != f.new_mode:
            res.seen["mode-change"] += 1
        if any(ord(ch) > 127 for ch in f.path):
            res.seen["non-ascii"] += 1
    res.bad += [f"{label}: {p}" for p in check_text_outputs(patch, raws, nums)]
    res.bad += [f"{label}: {p}" for p in check_binary(root, case.repo, patch, res)]
    res.bad += [f"{label}: {p}" for p in check_apply(root, case.repo, text, patch, res, rng, case.flags)]
    res.bad += [f"{label}: {p}" for p in check_mail(root, case, patch, res)]


def check(root: Path, only: str | None = None) -> Result:
    res = Result()
    rng = random.Random(1)
    tip = synthetic.build(root / "synthetic.git")
    for name, _url, sha, _depth in [*REPOS, ("synthetic", "", tip, 0)]:
        if only and name != only:
            continue
        for case in make_cases(root, name, sha):
            run_case(root, case, res, rng)
    return res


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("fetch", "check"):
        print(__doc__)
        return 2
    root = Path(argv[2])
    if argv[1] == "fetch":
        fetch(root)
        return 0
    res = check(root, argv[3] if len(argv) > 3 else None)
    print(f"patches            {res.patches}  {dict(res.kinds)}")
    print(f"file entries       {res.entries}")
    print(f"exact              {res.exact}")
    print(f"gap (info absent)  {res.gap}")
    print(f"mismatch           {len(res.bad)}")
    print(f"binary payloads    {res.binary_ok}/{res.binary_checked}")
    print(f"text apply         {res.apply_ok}/{res.apply_checked}")
    print(f"mail headers       {res.mail_ok}/{res.mail_checked}")
    print(f"combined diffs     {res.combined_ok}/{res.combined}")
    print(f"coverage           {dict(res.seen)}")
    groups: dict[str, list[str]] = {}
    for b in res.bad:
        key = re.sub(r"[0-9a-f]{10}|\d+|'[^']*'|\S*/\S*", "#", b.split(": ", 2)[-1])[:70]
        groups.setdefault(key, []).append(b)
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"BAD x{len(items)} {key}")
        for b in items[:3]:
            print("    ", b[:300])
    digest = hashlib.sha256("\n".join(sorted(res.bad)).encode()).hexdigest()[:12]
    print(f"bad-digest {digest}")
    return 0 if not res.bad else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
