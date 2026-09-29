# gitpatch

Pure-Python parser for the patches git produces (`git diff`, `git format-patch`, `git show`, `git log -p`), with no runtime dependencies.

```python
from gitpatch import parse

patch = parse(open("change.patch", "rb").read())
for f in patch.files:
    print(f.status, f.old_path, "->", f.new_path, f.old_mode, f.new_mode, f.similarity)
print(patch.numstat(z=True))
```

## Why

Parsers that read `---` / `+++` headers and hunks lose the entries that have neither: pure renames, mode changes, empty files, and binary patches. They also mishandle quoted names (`"a/\303\251.txt"`), names with spaces, and type changes. gitpatch parses git's extended headers and checks every file entry against `git diff --raw -z` and `--numstat -z`.

Measured with `python bench/incumbents.py CORPUS` on 264 `git diff -M -C --binary` patches (5,755 file entries) and 303 `git format-patch` patches (6,985 entries) from real repository history plus a synthetic repository of awkward names (git 2.55.0). A file entry counts as right when the parser reports the right paths, added/removed counts and change type. Incumbents receive credit for a match anywhere in their entry list.

```
git diff (-M/-C/-B, --binary, default prefixes): 264 patches, 5755 file entries
parser                         paths+counts          +status  patches right  crashed
gitpatch                     5755/5755 100.0%   5755/5755 100.0%     264/264        0
unidiff 1.0.1                5450/5755  94.7%   5440/5755  94.5%     205/264        9
whatthepatch 1.0.7           5079/5755  88.3%   5079/5755  88.3%     178/264        0
patch-ng 1.19.1              4874/5755  84.7%   4874/5755  84.7%     169/264        0
pygit2 1.20.1 (libgit2)      4910/5755  85.3%   4910/5755  85.3%     223/264       30

git format-patch: 303 patches, 6985 file entries
gitpatch                     6985/6985 100.0%   6985/6985 100.0%     303/303        0
unidiff 1.0.1                6928/6985  99.2%   6926/6985  99.2%     279/303        1
whatthepatch 1.0.7           6019/6985  86.2%   6019/6985  86.2%     175/303        0
patch-ng 1.19.1              5969/6985  85.5%   5969/6985  85.5%     238/303        0
pygit2 1.20.1 (libgit2)      5678/6985  81.3%   5678/6985  81.3%     281/303       17
```

Real histories are mostly plain text edits, which every parser reads. The difference is in the other kinds (`git diff` set, right entries by kind):

| Entry kind | Entries | gitpatch | unidiff | whatthepatch | pygit2 |
|---|---|---|---|---|---|
| plain text edit, add or delete | 4316 | 100.0% | 98.7% | 99.6% | 90.1% |
| binary | 586 | 100.0% | 96.6% | 25.3% | 49.5% |
| rename or copy | 514 | 100.0% | 100.0% | 100.0% | 97.9% |
| quoted, non-ASCII or spaced name | 184 | 100.0% | 9.8% | 29.3% | 91.3% |
| rename or copy, quoted or spaced name | 69 | 100.0% | 34.8% | 0.0% | 8.7% |
| header only (mode change, empty file) | 66 | 100.0% | 86.4% | 100.0% | 81.8% |
| type change | 20 | 100.0% | 0.0% | 0.0% | 0.0% |

On the synthetic repository alone (412 entries, awkward names throughout) unidiff scores 34.2% and whatthepatch 34.5%. The incumbents compared are unidiff, whatthepatch, patch-ng and pygit2 (`Diff.parse_diff`, a libgit2 binding, so not pure Python). diff-parser 1.1 documents that it returns no hunk content and was not scored. The fields the incumbents do not expose at all (modes, blob ids, similarity, binary payloads) are not part of these percentages.

## Installation

```
pip install gitpatch
```

Python 3.11 or newer. No dependencies.

## Usage

### Read file entries

```python
patch = parse(text_or_bytes)
for f in patch.files:
    f.old_path, f.new_path      # None for /dev/null; prefixes removed, quotes decoded
    f.status                    # added, deleted, modified, renamed, copied, type-changed
    f.similarity, f.dissimilarity
    f.old_mode, f.new_mode      # ints such as 0o100644
    f.old_hash, f.new_hash      # abbreviated or full ids from the index line
    for hunk in f.hunks:
        for line in hunk.lines:
            line.kind, line.content, line.old_lineno, line.new_lineno, line.no_newline
```

### Binary patches

```python
f = patch.files[0]
f.is_binary                     # True for "Binary files differ" and "GIT binary patch"
new = f.binary.forward.apply(old_bytes)   # literal or delta, base85 and zlib decoded
old = f.binary.reverse.apply(new_bytes)
```

`git diff --binary` patches carry both directions. A patch with only `Binary files a/x and b/x differ` has `is_binary` and no `binary` payload.

### format-patch and git show

```python
patch = parse(mbox_text)
for c in patch.commits:
    c.sha, c.author, c.date_parsed, c.title, c.message
patch.files[0].commit           # index into patch.commits
```

Mail headers are read up to the blank line, the message up to the `---` line, and the diffstat block is skipped. `git show` / `git log -p` headers (`commit`, `Author:`, `Date:`, indented message) are read the same way.

### Summaries that match git

```python
patch.raw(z=True)               # same bytes as `git diff --raw -z`
patch.numstat(z=True)           # same bytes as `git diff --numstat -z`
patch.numstat()                 # text form: quoted names, `dir/{old => new}/f` labels
```

### Apply

```python
new_bytes = f.apply(old_bytes)  # None for a deletion; pass None as the old content of an added file
```

Hunks must fit at their stated line numbers. There is no offset search and no fuzz. Binary payloads apply as well. `ApplyError` is raised on any mismatch.

### Prefixes

`a/`, `b/`, mnemonic prefixes (`c/`, `i/`, `w/`), `--no-prefix` and single-component `--src-prefix` / `--dst-prefix` values are detected from the `diff --git` line. Multi-component prefixes cannot be told apart from directories, so pass them:

```python
parse(text, src_prefix="x/y/", dst_prefix="z/y/")
```

## Reference

`parse(data, *, strip=1, src_prefix=None, dst_prefix=None, merge_type_changes=True) -> Patch`

| Name | Type | Default | Description |
|---|---|---|---|
| `data` | `str \| bytes` | required | Patch text. Bytes are kept exact; `str` is encoded as UTF-8. |
| `strip` | `int` | `1` | Leading path components removed from plain unified diffs (no `diff --git` header). |
| `src_prefix`, `dst_prefix` | `str \| None` | `None` | Explicit prefixes for `diff --git` entries. |
| `merge_type_changes` | `bool` | `True` | Merge the delete plus create git prints for a type change into one `type-changed` entry (its `parts` hold the two originals). |

`Patch`: `files: list[FileChange]`, `commits: list[CommitInfo]`, `raw(z=False)`, `numstat(z=False)`.

`FileChange`: `old_path`, `new_path`, `path`, `status`, `similarity`, `dissimilarity`, `old_mode`, `new_mode`, `old_hash`, `new_hash`, `is_binary`, `binary`, `hunks`, `is_combined`, `commit`, `line`, `end_line`, `parts`, `added`, `deleted`, `apply(content)`.

`Hunk`: `old_start`, `old_count`, `new_start`, `new_count`, `section`, `lines`, `added`, `deleted`. `HunkLine`: `kind` (`context`, `add`, `remove`), `content`, `old_lineno`, `new_lineno`, `no_newline`, `raw`. Text is decoded as UTF-8 with `surrogateescape`, so non-UTF-8 bytes round-trip.

`BinaryPatch`: `forward`, `reverse`. `BinaryData`: `kind` (`literal` or `delta`), `size`, `data`, `apply(base)`. `apply_delta(base, delta)` applies a pack-format delta.

`CommitInfo`: `kind` (`mbox` or `show`), `sha`, `author`, `date`, `date_parsed`, `subject`, `title`, `message`, `headers`, `line`.

`PatchError(message, line)`: subclass of `ValueError`; `line` is the 1-based line where the input stopped making sense. `ApplyError`: subclass of `ValueError`.

## How it works

The parser works on bytes and splits on `\n`. A `diff --git` line starts an entry. Extended header lines are read in the order git documents them (`old mode`, `new mode`, `deleted file mode`, `new file mode`, `copy from/to`, `rename from/to`, `similarity index`, `dissimilarity index`, `index`), then `---`/`+++` with hunks, `Binary files ... differ`, or `GIT binary patch`.

Hunks are read by count, not by looking for the next header, so a removed line that starts with `-- ` or a context line that looks like `diff --git` is content. Names with spaces are split by finding the two equal halves of the `diff --git` line (modulo the prefix); rename and copy lines are authoritative when present. Quoted names use git's C-style escapes with three-digit octal.

Binary payloads are base85 (RFC 1924 alphabet, with git's per-line length byte), zlib-inflated with a size cap, and checked against the declared size. Deltas use git's pack delta format.

`diff --cc` and `diff --combined` entries are detected and flagged `is_combined`; their bodies are skipped, so they cannot be mis-read as ordinary hunks.

The behavior of `git apply` and `git diff` was used as the oracle. No code from git is used; the parser is written from the documented patch format (git-diff(1), gitformat-patch) and from observed output.

## Limitations

- Combined diffs (`--cc`, `-c`) are detected but not parsed into hunks, and are skipped by `raw()`, `numstat()` and `apply()`.
- `raw()` prints zeros for values the patch does not contain: blob ids and modes of pure renames, copies and mode-only changes (git prints no `index` line for them).
- A pure rename or a mode-only change of a binary file has no `Binary files` line, so `is_binary` is false and `numstat()` prints `0 0` where git prints `- -`.
- `-B` scores on type changes (`T100` in `--raw`) are not in the patch.
- A multi-component `--src-prefix` / `--dst-prefix` needs the `src_prefix` / `dst_prefix` arguments. A patch made with `--no-prefix` whose file is also named `a/...` in the repository reads correctly only when both names are equal.
- `format-patch` mails using `--attach`, `--inline` or a transfer encoding other than 8bit are not decoded. `[PATCH]` tag stripping in `title` recognizes bracket groups containing `PATCH`.
- `apply()` requires exact line positions, no whitespace options, no 3-way merge, no `--reverse`.
- Localized `\ No newline at end of file` markers are read as markers whatever their text (any line starting with a backslash).
- ANSI-colored diffs are not supported.

## Compatibility

Run here: Python 3.14.7 on macOS, git 2.55.0. The CI workflow runs Python 3.11, 3.12, 3.13 and 3.14 on Linux; those runs are not part of this measurement. The oracle tests need `git` on the path and skip loudly without it.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy --strict src tests tools bench
```

The real-repository corpus is not stored in this repository. It is fetched from pinned commits (`REPOS` and `COMMIT_LISTS` in `tools/corpus.py`) into a directory of your choice:

```
python tools/corpus.py fetch corpus/
python tools/corpus.py check corpus/            # compares every file entry with git
GITPATCH_CORPUS=corpus/ python -m pytest -q     # the same check as a test
```

Repositories: cpython, git, django, vim, libgit2, pdf.js (history from a pinned tip) and php-src (26 pinned commits that add or change files with non-ASCII names), plus a synthetic repository built by `tools/synthetic.py`. Measured on the pinned corpus with git 2.55.0:

```
patches            874  {'diff': 302, 'format-patch': 303, 'show': 90, 'range': 71, 'log': 72, 'show-cc': 30, 'show-m': 6}
file entries       18809
exact              17537
gap (info absent)  1272
mismatch           0
binary payloads    1884/1884
text apply         15392/15392
mail headers       538/538
combined diffs     30/30
```

`exact` entries match `--raw -z` and `--numstat -z` in every field. `gap` entries match in every field the patch carries; the remaining field is one that the patch cannot contain (see Limitations). Binary payloads decode to the exact blob bytes from `git cat-file` (delta payloads are applied to the old blob, and reverse payloads are checked too). Text `apply()` results equal the new blob; a sample was also compared with `git apply`. Patches over 3 MB or 250 file entries are skipped. The mutation and random-input fuzz tests (`tests/test_fuzz.py`) require every input to parse or raise `PatchError` with a line number.

To rerun the incumbent comparison, install unidiff, whatthepatch, patch-ng and pygit2 into a separate environment and run `python bench/incumbents.py corpus/`.

## License

MIT. See [LICENSE](LICENSE).
