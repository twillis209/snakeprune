import pytest
from pathlib import Path

from snakeprune.delete import delete_orphans
from snakeprune.walker import OrphanFile


def test_delete_orphans_unlinks_regular_files(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("data")
    delete_orphans([OrphanFile(path=f)], allow_symlinks=False)
    assert not f.exists()


def test_delete_orphans_refuses_symlink_by_default(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        delete_orphans([OrphanFile(path=link)], allow_symlinks=False)
    # Both still exist
    assert target.exists()
    assert link.is_symlink()


def test_delete_orphans_allows_symlink_with_flag(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    delete_orphans([OrphanFile(path=link)], allow_symlinks=True)
    assert not link.is_symlink()
    # Target untouched (we only unlinked the symlink itself)
    assert target.exists()


def test_delete_orphans_refuses_directories(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        delete_orphans([OrphanFile(path=d)], allow_symlinks=False)
    assert d.is_dir()
