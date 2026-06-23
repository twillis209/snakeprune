import pytest
from pathlib import Path

from snakeprune.delete import delete_orphans
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
