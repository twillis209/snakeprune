import pytest
import shutil
from pathlib import Path

from snakeprune.delete import DeletionFailed, delete_orphans
from snakeprune.walker import OrphanFile


def test_delete_orphans_unlinks_regular_files(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("data")
    delete_orphans([OrphanFile(path=f, rel="dummy")], allow_symlinks=False)
    assert not f.exists()


def test_delete_orphans_refuses_symlink_by_default(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        delete_orphans([OrphanFile(path=link, rel="dummy")], allow_symlinks=False)
    # Both still exist
    assert target.exists()
    assert link.is_symlink()


def test_delete_orphans_allows_symlink_with_flag(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    delete_orphans([OrphanFile(path=link, rel="dummy")], allow_symlinks=True)
    assert not link.is_symlink()
    # Target untouched (we only unlinked the symlink itself)
    assert target.exists()


def test_delete_orphans_refuses_directories(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        delete_orphans([OrphanFile(path=d, rel="dummy")], allow_symlinks=False)
    assert d.is_dir()


def test_delete_orphans_trash_moves_files_to_dir(tmp_path):
    src = tmp_path / "results" / "sub"
    src.mkdir(parents=True)
    f = src / "x.txt"
    f.write_text("data")
    trash = tmp_path / "trash"
    delete_orphans(
        [OrphanFile(path=f, rel="sub/x.txt")],
        allow_symlinks=False,
        trash_dir=trash,
        results_dir_name="results",
    )
    # Original gone, file relocated with full rel structure under <trash>/<results_dir_name>/.
    assert not f.exists()
    assert (trash / "results" / "sub" / "x.txt").read_text() == "data"


def test_delete_orphans_trash_creates_target_dir_if_missing(tmp_path):
    src = tmp_path / "results"
    src.mkdir()
    f = src / "a.txt"
    f.write_text("data")
    trash = tmp_path / "does_not_exist_yet"
    delete_orphans(
        [OrphanFile(path=f, rel="a.txt")],
        trash_dir=trash,
        results_dir_name="results",
    )
    assert (trash / "results" / "a.txt").exists()


def test_delete_orphans_trash_refuses_symlink_without_flag(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "results" / "link.txt"
    link.parent.mkdir()
    link.symlink_to(target)
    trash = tmp_path / "trash"
    with pytest.raises(PermissionError):
        delete_orphans(
            [OrphanFile(path=link, rel="link.txt")],
            allow_symlinks=False,
            trash_dir=trash,
            results_dir_name="results",
        )
    assert link.is_symlink()


def test_delete_orphans_trash_requires_results_dir_name(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("data")
    trash = tmp_path / "trash"
    with pytest.raises(ValueError):
        delete_orphans(
            [OrphanFile(path=f, rel="x.txt")],
            trash_dir=trash,
            results_dir_name=None,
        )


def test_delete_orphans_validates_whole_batch_before_deleting_any(tmp_path):
    # All-or-nothing: a single bad orphan (a symlink without the flag) must abort
    # the entire batch *before* any valid orphan is removed. Otherwise a bad
    # entry late in the list silently destroys the files listed before it.
    good = tmp_path / "good.txt"
    good.write_text("data")
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        delete_orphans(
            [
                OrphanFile(path=good, rel="good.txt"),
                OrphanFile(path=link, rel="link.txt"),
            ],
            allow_symlinks=False,
        )
    # The valid file must survive: validation happens before any unlink.
    assert good.exists()
    assert link.is_symlink()


def test_delete_orphans_validates_directory_before_deleting_any(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("data")
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        delete_orphans(
            [
                OrphanFile(path=good, rel="good.txt"),
                OrphanFile(path=d, rel="subdir"),
            ],
            allow_symlinks=False,
        )
    assert good.exists()
    assert d.is_dir()


def test_delete_orphans_trash_refuses_when_target_exists(tmp_path):
    # A prior run already left a file at the trash target. Moving a new orphan
    # to the same relative path must refuse rather than silently overwrite the
    # previously-trashed copy (which shutil.move would do on POSIX).
    src_dir = tmp_path / "results"
    src_dir.mkdir()
    f = src_dir / "a.txt"
    f.write_text("new")
    trash = tmp_path / "trash"
    prior = trash / "results" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("old")
    with pytest.raises(FileExistsError):
        delete_orphans(
            [OrphanFile(path=f, rel="a.txt")],
            trash_dir=trash,
            results_dir_name="results",
        )
    # Neither the source nor the previously-trashed copy is touched.
    assert f.read_text() == "new"
    assert prior.read_text() == "old"


def test_delete_orphans_trash_collision_check_is_all_or_nothing(tmp_path):
    # The collision check runs in the pre-flight pass, so a colliding orphan
    # late in the batch aborts before any earlier orphan has been moved.
    src_dir = tmp_path / "results"
    src_dir.mkdir()
    first = src_dir / "first.txt"
    first.write_text("first")
    collide = src_dir / "collide.txt"
    collide.write_text("new")
    trash = tmp_path / "trash"
    prior = trash / "results" / "collide.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("old")
    with pytest.raises(FileExistsError):
        delete_orphans(
            [
                OrphanFile(path=first, rel="first.txt"),
                OrphanFile(path=collide, rel="collide.txt"),
            ],
            trash_dir=trash,
            results_dir_name="results",
        )
    # The non-colliding orphan must still be in place.
    assert first.read_text() == "first"
    assert not (trash / "results" / "first.txt").exists()


def test_delete_orphans_mid_batch_failure_reports_progress(tmp_path, monkeypatch):
    # Validation is all-or-nothing, but once unlinking starts a failure can
    # strike half-way. The caller must learn exactly how many files are already
    # gone — "Refusing to delete" after the fact would be a lie.
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    for f in (first, second, third):
        f.write_text("data")
    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == second:
            raise PermissionError(f"[Errno 13] Permission denied: '{self}'")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.raises(DeletionFailed) as exc:
        delete_orphans(
            [
                OrphanFile(path=first, rel="first.txt"),
                OrphanFile(path=second, rel="second.txt"),
                OrphanFile(path=third, rel="third.txt"),
            ]
        )
    assert exc.value.completed == 1
    assert exc.value.total == 3
    assert exc.value.path == second
    assert "1 of 3" in str(exc.value)
    assert not first.exists()
    assert second.exists()
    assert third.exists()


def test_delete_orphans_trash_mid_batch_failure_reports_progress(tmp_path, monkeypatch):
    # Same honesty requirement in trash mode: a failed move part-way must say
    # how many files were already relocated and leave the rest untouched.
    import shutil as _shutil
    src = tmp_path / "src"
    src.mkdir()
    a = src / "a.txt"
    b = src / "b.txt"
    a.write_text("a")
    b.write_text("b")
    trash = tmp_path / "trash"
    real_move = _shutil.move

    def flaky_move(s, d, *args, **kwargs):
        if str(s) == str(b):
            raise OSError("disk full")
        return real_move(s, d, *args, **kwargs)

    monkeypatch.setattr("snakeprune.delete.shutil.move", flaky_move)
    with pytest.raises(DeletionFailed) as exc:
        delete_orphans(
            [OrphanFile(path=a, rel="a.txt"), OrphanFile(path=b, rel="b.txt")],
            trash_dir=trash,
            results_dir_name="src",
        )
    assert exc.value.completed == 1
    assert (trash / "src" / "a.txt").exists()
    assert b.exists()


def test_delete_orphans_trash_refuses_when_ancestor_is_a_file(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    z = src / "z.txt"
    z.write_text("z")
    b = src / "a" / "b.txt"
    b.write_text("b")
    trash = tmp_path / "trash"
    (trash / "src").mkdir(parents=True)
    (trash / "src" / "a").write_text("blocker")
    with pytest.raises(FileExistsError, match="not a directory"):
        delete_orphans(
            [OrphanFile(path=z, rel="z.txt"), OrphanFile(path=b, rel="a/b.txt")],
            trash_dir=trash,
            results_dir_name="src",
        )
    assert z.exists()
    assert b.exists()
    assert not (trash / "src" / "z.txt").exists()


def test_delete_orphans_trash_allows_existing_ancestor_directory(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    b = src / "a" / "b.txt"
    b.write_text("b")
    trash = tmp_path / "trash"
    (trash / "src" / "a").mkdir(parents=True)
    delete_orphans(
        [OrphanFile(path=b, rel="a/b.txt")],
        trash_dir=trash,
        results_dir_name="src",
    )
    assert not b.exists()
    assert (trash / "src" / "a" / "b.txt").read_text() == "b"


def test_delete_orphans_trash_refuses_when_trash_dir_is_a_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    z = src / "z.txt"
    z.write_text("z")
    trash = tmp_path / "trash"
    trash.write_text("blocker")
    with pytest.raises(FileExistsError, match="not a directory"):
        delete_orphans(
            [OrphanFile(path=z, rel="z.txt")],
            trash_dir=trash,
            results_dir_name="src",
        )
    assert z.exists()
    assert trash.read_text() == "blocker"


def test_delete_orphans_trash_refuses_dangling_symlink_ancestor(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    b = src / "a" / "b.txt"
    b.write_text("b")
    trash = tmp_path / "trash"
    (trash / "src").mkdir(parents=True)
    (trash / "src" / "a").symlink_to(tmp_path / "nowhere")
    with pytest.raises(FileExistsError, match="not a directory"):
        delete_orphans(
            [OrphanFile(path=b, rel="a/b.txt")],
            trash_dir=trash,
            results_dir_name="src",
        )
    assert b.exists()


class _Reiterable:
    """Re-iterable, like the CLI's spooled selection; never a list."""

    def __init__(self, items):
        self._items = items
        self.passes = 0

    def __iter__(self):
        self.passes += 1
        return iter(list(self._items))


def test_delete_orphans_reiterable_validation_failure_deletes_nothing(tmp_path):
    from snakeprune.delete import delete_orphans
    from snakeprune.walker import OrphanFile

    good = [tmp_path / f"f{i}.txt" for i in range(3)]
    for g in good:
        g.write_text("x")
    bad = tmp_path / "dir"
    bad.mkdir()
    items = _Reiterable(
        [OrphanFile(path=g, rel=g.name) for g in good] + [OrphanFile(path=bad, rel="dir")]
    )
    with pytest.raises(IsADirectoryError):
        delete_orphans(items)
    assert all(g.exists() for g in good)


def test_delete_orphans_reiterable_deletes_all(tmp_path):
    from snakeprune.delete import delete_orphans
    from snakeprune.walker import OrphanFile

    files = [tmp_path / f"f{i}.txt" for i in range(3)]
    for f in files:
        f.write_text("x")
    items = _Reiterable([OrphanFile(path=f, rel=f.name) for f in files])
    delete_orphans(items)
    assert not any(f.exists() for f in files)
    assert items.passes >= 2  # validated in full, then acted on


class _FlakyOrphans:
    """Re-iterable orphan list whose read-back fails on a chosen pass."""

    def __init__(self, orphans, fail_pass, fail_after):
        self._orphans = orphans
        self._fail_pass = fail_pass
        self._fail_after = fail_after
        self.passes = 0

    def __iter__(self):
        self.passes += 1
        this_pass = self.passes
        for i, o in enumerate(self._orphans):
            if this_pass == self._fail_pass and i == self._fail_after:
                raise OSError("orphan spool is truncated")
            yield o


def test_delete_orphans_read_failure_mid_action_reports_progress(tmp_path):
    # The deletion list is read back from disk while files are being removed;
    # a read failure then is a mid-batch failure (some files gone), not a
    # validation failure, and must say how many were removed.
    files = [tmp_path / f"{n}.txt" for n in "abc"]
    for f in files:
        f.write_text("x")
    orphans = _FlakyOrphans(
        [OrphanFile(path=f, rel=f.name) for f in files], fail_pass=2, fail_after=1
    )
    with pytest.raises(DeletionFailed) as exc:
        delete_orphans(orphans)
    assert exc.value.completed == 1
    assert "1 of 3" in str(exc.value)
    assert not files[0].exists()
    assert files[1].exists() and files[2].exists()


def test_delete_orphans_read_failure_during_validation_is_plain_oserror(tmp_path):
    files = [tmp_path / f"{n}.txt" for n in "ab"]
    for f in files:
        f.write_text("x")
    orphans = _FlakyOrphans(
        [OrphanFile(path=f, rel=f.name) for f in files], fail_pass=1, fail_after=1
    )
    with pytest.raises(OSError) as exc:
        delete_orphans(orphans)
    assert not isinstance(exc.value, DeletionFailed)
    assert all(f.exists() for f in files)


def test_delete_orphans_trash_failure_message_says_moved(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    a, b = src / "a.txt", src / "b.txt"
    a.write_text("a")
    b.write_text("b")
    real_move = shutil.move

    def flaky_move(s, d, *args, **kwargs):
        if str(s) == str(b):
            raise OSError("disk full")
        return real_move(s, d, *args, **kwargs)

    monkeypatch.setattr("snakeprune.delete.shutil.move", flaky_move)
    with pytest.raises(DeletionFailed) as exc:
        delete_orphans(
            [OrphanFile(path=a, rel="a.txt"), OrphanFile(path=b, rel="b.txt")],
            trash_dir=tmp_path / "trash",
            results_dir_name="src",
        )
    msg = str(exc.value)
    assert "Moved 1 of 2" in msg
    assert "moving to trash:" in msg
    assert "Deleted" not in msg
