from __future__ import annotations

import base64
import zlib

import pytest

from gitpatch import ApplyError, PatchError, apply_delta, parse

BASIC = """\
diff --git a/f.txt b/f.txt
index de98044..7be73ce 100644
--- a/f.txt
+++ b/f.txt
@@ -1,3 +1,3 @@ def f():
 a
-b
+B
 c
"""


def test_modified_file() -> None:
    p = parse(BASIC)
    (f,) = p.files
    assert (f.old_path, f.new_path, f.status) == ("f.txt", "f.txt", "modified")
    assert (f.old_mode, f.new_mode) == (0o100644, 0o100644)
    assert (f.old_hash, f.new_hash) == ("de98044", "7be73ce")
    (h,) = f.hunks
    assert (h.old_start, h.old_count, h.new_start, h.new_count, h.section) == (1, 3, 1, 3, "def f():")
    assert [(ln.kind, ln.content, ln.old_lineno, ln.new_lineno) for ln in h.lines] == [
        ("context", "a", 1, 1),
        ("remove", "b", 2, None),
        ("add", "B", None, 2),
        ("context", "c", 3, 3),
    ]
    assert (f.added, f.deleted) == (1, 1)


def test_bytes_and_str_agree() -> None:
    assert parse(BASIC.encode()).files[0].path == parse(BASIC).files[0].path


def test_added_and_deleted_use_dev_null() -> None:
    text = (
        "diff --git a/n b/n\nnew file mode 100755\nindex 0000000..e69de29\n"
        "diff --git a/d b/d\ndeleted file mode 100644\nindex 3b18e51..0000000\n"
        "--- a/d\n+++ /dev/null\n@@ -1 +0,0 @@\n-hello\n"
    )
    added, deleted = parse(text).files
    assert (added.status, added.old_path, added.new_path, added.new_mode) == ("added", None, "n", 0o100755)
    assert added.new_hash == "e69de29" and added.old_hash is None
    assert (deleted.status, deleted.old_path, deleted.new_path) == ("deleted", "d", None)
    assert deleted.path == "d"


def test_rename_copy_and_similarity() -> None:
    text = (
        "diff --git a/old name b/new name\nsimilarity index 85%\nrename from old name\nrename to new name\n"
        "diff --git a/x b/y\nsimilarity index 66%\ncopy from x\ncopy to y\nindex de98044..7be73ce 100644\n"
        "--- a/x\n+++ b/y\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/p b/q\ndissimilarity index 90%\nindex 1111111..2222222 100644\n--- a/p\n+++ b/q\n@@ -1 +1 @@\n-a\n+b\n"
    )
    ren, cp, mod = parse(text).files
    assert (ren.status, ren.old_path, ren.new_path, ren.similarity) == ("renamed", "old name", "new name", 85)
    assert (cp.status, cp.old_path, cp.new_path, cp.similarity) == ("copied", "x", "y", 66)
    assert mod.dissimilarity == 90 and mod.status == "modified"


def test_mode_change_only() -> None:
    (f,) = parse("diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n").files
    assert (f.old_mode, f.new_mode, f.status, f.hunks) == (0o100644, 0o100755, "modified", [])


@pytest.mark.parametrize(
    ("header", "old", "new"),
    [
        ('diff --git "a/\\303\\251.txt" "b/\\303\\251.txt"', "é.txt", "é.txt"),
        ("diff --git a/sp ace.txt b/sp ace.txt", "sp ace.txt", "sp ace.txt"),
        ("diff --git a/a b/c b/a b/c", "a b/c", "a b/c"),
        ('diff --git "a/ta\\tb" "b/ta\\tb"', "ta\tb", "ta\tb"),
        ('diff --git "a/q\\"uote" "b/q\\"uote"', 'q"uote', 'q"uote'),
        ("diff --git x/y x/y", "x/y", "x/y"),
    ],
)
def test_header_names(header: str, old: str, new: str) -> None:
    (f,) = parse(header + "\nold mode 100644\nnew mode 100755\n").files
    assert (f.old_path, f.new_path) == (old, new)


def test_no_prefix_when_names_equal() -> None:
    (f,) = parse("diff --git dir/f dir/f\nindex 1111111..2222222 100644\n--- dir/f\n+++ dir/f\n@@ -1 +1 @@\n-a\n+b\n").files
    assert f.path == "dir/f"


def test_explicit_prefixes_override_detection() -> None:
    text = "diff --git x/y/deps/f z/y/deps/f\nindex 1111111..2222222 100644\n--- x/y/deps/f\n+++ z/y/deps/f\n@@ -1 +1 @@\n-a\n+b\n"
    assert parse(text).files[0].path == "y/deps/f"
    assert parse(text, src_prefix="x/y/", dst_prefix="z/y/").files[0].path == "deps/f"


def test_quoted_names_in_rename_and_body() -> None:
    text = (
        'diff --git "a/\\303\\251.txt" "b/\\303\\2512.txt"\nsimilarity index 100%\n'
        'rename from "\\303\\251.txt"\nrename to "\\303\\2512.txt"\n'
        'diff --git "a/\\303\\251.txt" "b/\\303\\251.txt"\nindex 1111111..2222222 100644\n'
        '--- "a/\\303\\251.txt"\n+++ "b/\\303\\251.txt"\n@@ -1 +1 @@\n-a\n+b\n'
    )
    ren, mod = parse(text).files
    assert (ren.old_path, ren.new_path) == ("é.txt", "é2.txt")
    assert mod.path == "é.txt"


def test_no_newline_marker_and_zero_length_ranges() -> None:
    text = "diff --git a/n b/n\nindex 1111111..2222222 100644\n--- a/n\n+++ b/n\n@@ -1 +1 @@\n-no nl\n\\ No newline at end of file\n+no nl!\n\\ No newline at end of file\n"
    (h,) = parse(text).files[0].hunks
    assert [ln.no_newline for ln in h.lines] == [True, True]
    assert h.lines[0].raw == b"no nl" and h.lines[1].raw == b"no nl!"


def test_hunk_body_lines_that_look_like_headers() -> None:
    text = (
        "diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n"
        "--- a/not/a/header\n+++ b/not/a/header\ndiff --git a/g b/g\n"
    )
    # 'diff --git a/g b/g' at column 0 after a complete hunk starts a new entry
    files = parse(text.replace("--- a/not/a/header\n+++ b/not/a/header\n", "--- x\n+++ y\n")).files
    assert [f.path for f in files] == ["f", "g"]
    assert [ln.content for ln in files[0].hunks[0].lines] == ["-- x", "++ y"]


def test_type_change_is_merged_and_can_be_split() -> None:
    text = (
        "diff --git a/l b/l\ndeleted file mode 120000\nindex 7f66e4f..0000000\n--- a/l\n+++ /dev/null\n@@ -1 +0,0 @@\n-f.txt\n\\ No newline at end of file\n"
        "diff --git a/l b/l\nnew file mode 100644\nindex 0000000..3f899ea\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+now a file\n"
    )
    (t,) = parse(text).files
    assert (t.status, t.old_mode, t.new_mode, t.added, t.deleted) == ("type-changed", 0o120000, 0o100644, 1, 1)
    assert len(t.parts) == 2
    assert [f.status for f in parse(text, merge_type_changes=False).files] == ["deleted", "added"]
    assert t.apply(b"f.txt") == b"now a file\n"


def test_binary_files_differ_line() -> None:
    (f,) = parse("diff --git a/x.png b/x.png\nindex 1111111..2222222 100644\nBinary files a/x.png and b/x.png differ\n").files
    assert f.is_binary and f.binary is None


def _b85_block(kind: str, payload: bytes) -> str:
    z = zlib.compress(payload)
    lines = []
    for i in range(0, len(z), 52):
        chunk = z[i : i + 52]
        n = len(chunk)
        ch = chr(ord("A") + n - 1) if n <= 26 else chr(ord("a") + n - 27)
        lines.append(ch + base64.b85encode(chunk + b"\0" * (-n % 4)).decode())
    return f"{kind} {len(payload)}\n" + "\n".join(lines) + "\n\n"


def test_binary_literal_and_delta() -> None:
    new = bytes(range(200))
    old = b"abcdefgh"
    delta = bytes([8, 7, 0x91, 4, 4, 3]) + b"xyz"  # copy 4 bytes from offset 4, insert "xyz"
    text = (
        "diff --git a/b b/b\nindex 1111111..2222222 100644\nGIT binary patch\n"
        + _b85_block("literal", new)
        + _b85_block("delta", delta)
    )
    (f,) = parse(text).files
    assert f.binary is not None and f.is_binary
    assert f.binary.forward.kind == "literal" and f.binary.forward.data == new
    assert f.binary.reverse is not None and f.binary.reverse.kind == "delta"
    assert f.binary.reverse.apply(old) == b"efghxyz"
    assert apply_delta(old, delta) == b"efghxyz"
    assert f.apply(old) == new


def test_apply_delta_errors() -> None:
    with pytest.raises(ApplyError):
        apply_delta(b"abc", bytes([4, 1, 1, 0x41]))
    with pytest.raises(ApplyError):
        apply_delta(b"abc", bytes([3, 1, 0x00]))


def test_binary_declared_size_mismatch() -> None:
    text = "diff --git a/b b/b\nindex 1111111..2222222 100644\nGIT binary patch\n" + _b85_block("literal", b"abc").replace("literal 3", "literal 9")
    with pytest.raises(PatchError) as exc:
        parse(text)
    assert exc.value.line == 4


def test_combined_diff_is_detected_not_parsed() -> None:
    text = (
        "diff --cc f.txt\nindex 750d243,892d35b..a13cc60\n--- a/f.txt\n+++ b/f.txt\n@@@ -1,2 -1,2 +1,2 @@@\n"
        "  common\n- ours\n -theirs\n++merged\n"
        "diff --git a/g b/g\nold mode 100644\nnew mode 100755\n"
    )
    c, g = parse(text).files
    assert c.is_combined and c.path == "f.txt" and c.hunks == []
    assert not g.is_combined and g.path == "g"
    assert parse(text).raw() == ":100644 100755 0000000 0000000 M\tg\n"
    with pytest.raises(ApplyError):
        c.apply(b"")


def test_format_patch_mail_and_stat_block() -> None:
    text = (
        "From 0123456789abcdef0123456789abcdef01234567 Mon Sep 17 00:00:00 2001\n"
        "From: =?UTF-8?q?Andr=C3=A9?= <a@example.com>\n"
        "Date: Sat, 5 Sep 2026 12:00:00 +0200\n"
        "Subject: [PATCH 2/3] Fix a very long subject line that\n was folded by the mailer\n\n"
        "Body line one.\n\nSigned-off-by: A <a@example.com>\n---\n f.txt | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n\n"
        + BASIC
        + "-- \n2.39.0\n\n"
        "From fedcba9876543210fedcba9876543210fedcba98 Mon Sep 17 00:00:00 2001\nFrom: B <b@example.com>\nSubject: [PATCH] second\n\nBody.\n---\n"
        + BASIC.replace("f.txt", "g.txt")
    )
    p = parse(text)
    assert [f.path for f in p.files] == ["f.txt", "g.txt"]
    assert [f.commit for f in p.files] == [0, 1]
    first, second = p.commits
    assert first.sha == "0123456789abcdef0123456789abcdef01234567"
    assert first.author == "André <a@example.com>"
    assert first.subject == "[PATCH 2/3] Fix a very long subject line that was folded by the mailer"
    assert first.title == "Fix a very long subject line that was folded by the mailer"
    assert first.message == "Body line one.\n\nSigned-off-by: A <a@example.com>"
    dt = first.date_parsed
    assert dt is not None and dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 7200  # type: ignore[union-attr]
    assert second.title == "second"


def test_git_show_header() -> None:
    text = (
        "commit 0123456789abcdef0123456789abcdef01234567 (HEAD -> main)\nAuthor: A B <a@example.com>\n"
        "Date:   Mon Sep 17 00:00:00 2001 -0700\n\n    Subject line\n\n    More text.\n\n" + BASIC
    )
    p = parse(text)
    (c,) = p.commits
    assert (c.kind, c.author, c.subject, c.message) == ("show", "A B <a@example.com>", "Subject line", "Subject line\n\nMore text.")
    assert c.date_parsed is not None and c.date_parsed.utcoffset().total_seconds() == -7 * 3600  # type: ignore[union-attr]
    assert p.files[0].commit == 0


def test_traditional_unified_diff() -> None:
    text = "--- a/old.txt\t2020-01-01 00:00:00\n+++ b/new.txt\t2020-01-02 00:00:00\n@@ -1 +1 @@\n-a\n+b\n--- /dev/null\n+++ b/created\n@@ -0,0 +1 @@\n+x\n"
    a, b = parse(text).files
    assert (a.old_path, a.new_path, a.status) == ("old.txt", "new.txt", "modified")
    assert (b.status, b.new_path) == ("added", "created")
    assert parse(text, strip=0).files[0].old_path == "a/old.txt"


def test_summaries_match_git_format() -> None:
    p = parse(BASIC)
    assert p.raw() == ":100644 100644 de98044 7be73ce M\tf.txt\n"
    assert p.raw(z=True) == ":100644 100644 de98044 7be73ce M\0f.txt\0"
    assert p.numstat() == "1\t1\tf.txt\n"
    assert p.numstat(z=True) == "1\t1\tf.txt\0"


def test_summary_quoting_and_rename_labels() -> None:
    text = (
        'diff --git "a/\\303\\251.txt" "b/d/\\303\\251.txt"\nsimilarity index 100%\nrename from "\\303\\251.txt"\nrename to "d/\\303\\251.txt"\n'
    )
    p = parse(text)
    assert p.raw().endswith('R100\t"\\303\\251.txt"\t"d/\\303\\251.txt"\n')
    assert p.raw(z=True).endswith("R100\0é.txt\0d/é.txt\0")
    assert p.numstat(z=True) == "0\t0\t\0é.txt\0d/é.txt\0"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n", 6),
        ("diff --git a/f b/f\n--- a/f\n", 3),
        ("diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n@@ nonsense @@\n", 5),
        ("diff --git a/f b/f\nold mode 12z\n", 2),
        ("diff --git a/f b/f\nrename from x\n", 1),
        ("diff --git a/f b/f\nindex zz\n", 2),
        ('diff --git "a/x\\9" "b/x"\nold mode 100644\n', 1),
        ("diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n-b\n", 7),
    ],
)
def test_errors_carry_line_numbers(text: str, line: int) -> None:
    with pytest.raises(PatchError) as exc:
        parse(text)
    assert exc.value.line == line
    assert f"line {line}" in str(exc.value)


def test_garbage_and_empty_input_yield_no_files() -> None:
    assert parse("").files == []
    assert parse("hello\nworld\n").files == []
    assert parse(b"\xff\xfe\x00").files == []


def test_apply_text_and_mismatch() -> None:
    (f,) = parse(BASIC).files
    assert f.apply(b"a\nb\nc\n") == b"a\nB\nc\n"
    with pytest.raises(ApplyError):
        f.apply(b"a\nX\nc\n")
    with pytest.raises(ApplyError):
        f.apply(None)


def test_non_utf8_bytes_survive() -> None:
    text = b"diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-\xff\n+\xfe\n"
    (f,) = parse(text).files
    assert f.apply(b"\xff\n") == b"\xfe\n"
