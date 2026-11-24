import os

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


def test_existing_directory_entry_is_literal_even_with_glob_chars(tmp_path):
    (tmp_path / "run[1]").mkdir()
    (tmp_path / "ad_hoc").mkdir()
    ig = load_ignore_file(_write(tmp_path, "run[1]/\nad_hoc\n"))
    assert [d for _, d in ig.dirs] == ["run[1]", "ad_hoc"]
    assert ig.globs == []


def test_too_many_double_stars_names_the_line(tmp_path):
    with pytest.raises(IgnoreFileError, match="line 2"):
        load_ignore_file(_write(tmp_path, "ok\na/**/b/**/c/**/d/**/x\n"))


def test_ancestor_ignore_file_found_through_symlinked_directory(tmp_path):
    # Scanning project/results where `results` is a symlink to storage
    # elsewhere: project/.snakepruneignore is an ancestor lexically, though
    # not of the resolved path, and must still be found.
    from snakeprune.ignorefile import find_ancestor_ignore_files

    storage = tmp_path / "storage" / "results"
    storage.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".snakepruneignore").write_text("keep/\n")
    (project / "results").symlink_to(storage)
    found = find_ancestor_ignore_files(project / "results")
    assert project / ".snakepruneignore" in found


def test_ancestor_ignore_file_found_by_resolved_path(tmp_path):
    from snakeprune.ignorefile import find_ancestor_ignore_files

    real = tmp_path / "real"
    (real / "results").mkdir(parents=True)
    (real / ".snakepruneignore").write_text("")
    link = tmp_path / "link"
    link.mkdir()
    (link / "results").symlink_to(real / "results")
    found = find_ancestor_ignore_files(link / "results")
    assert any(os.path.samefile(p, real / ".snakepruneignore") for p in found)


def test_ancestor_ignore_file_case_variant_is_found(tmp_path):
    from snakeprune.ignorefile import find_ancestor_ignore_files

    (tmp_path / "results").mkdir()
    (tmp_path / ".SnakepruneIgnore").write_text("")
    found = find_ancestor_ignore_files(tmp_path / "results")
    assert [p.name for p in found if p.parent == tmp_path] == [".SnakepruneIgnore"]
