import pytest

from snakeprune.ignorefile import IgnoreFileError, load_ignore_file


def _write(tmp_path, text, mode="text"):
    f = tmp_path / ".snakepruneignore"
    if mode == "bytes":
        f.write_bytes(text)
    else:
        f.write_text(text)
    return tmp_path


def test_absent_file_is_none(tmp_path):
    assert load_ignore_file(tmp_path) is None


def test_parses_dirs_globs_and_glob_dirs(tmp_path):
    ig = load_ignore_file(_write(
        tmp_path,
        "# keep these\n\n  _old_snapshot/  \nnotes/**\n*.log\n_*/\nweird[1]/\n",
    ))
    assert [(e.line, d) for e, d in ig.dirs] == [(3, "_old_snapshot")]
    assert [(e.line, g) for e, g in ig.globs] == [
        (4, "notes/**"), (5, "*.log"), (6, "_*/*"), (7, "weird[1]/*"),
    ]
    assert ig.entry_count == 5


@pytest.mark.parametrize("line", ["!keep.txt", "/abs/", "../up/", "/"])
def test_refuses_entries_it_will_not_guess_about(tmp_path, line):
    with pytest.raises(IgnoreFileError, match="line 1"):
        load_ignore_file(_write(tmp_path, line + "\n"))


def test_refuses_undecodable_file(tmp_path):
    with pytest.raises(IgnoreFileError, match="cannot read"):
        load_ignore_file(_write(tmp_path, b"\xff\xfe\n", mode="bytes"))


def test_refuses_symlinked_or_non_file(tmp_path):
    (tmp_path / "real").write_text("x/\n")
    (tmp_path / ".snakepruneignore").symlink_to(tmp_path / "real")
    with pytest.raises(IgnoreFileError, match="not a regular file"):
        load_ignore_file(tmp_path)


def test_refuses_directory_named_like_the_file(tmp_path):
    (tmp_path / ".snakepruneignore").mkdir()
    with pytest.raises(IgnoreFileError, match="not a regular file"):
        load_ignore_file(tmp_path)
