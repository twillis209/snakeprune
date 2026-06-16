# tests/conftest.py
from pathlib import Path
import pytest


def _make_pipeline(tmp_path: Path, snakefile_text: str, smk_files: dict[str, str] | None = None) -> Path:
    """Create a synthetic pipeline directory with given Snakefile content and optional .smk files."""
    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()
    (pipeline_dir / "Snakefile").write_text(snakefile_text)
    for name, content in (smk_files or {}).items():
        target = pipeline_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return pipeline_dir


def _make_results(tmp_path: Path, files: list[str]) -> Path:
    """Create a synthetic results directory with the given relative file paths (all empty)."""
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    for rel in files:
        full = results_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.touch()
    return results_dir


@pytest.fixture
def make_pipeline(tmp_path):
    def _factory(snakefile_text: str, smk_files: dict[str, str] | None = None) -> Path:
        return _make_pipeline(tmp_path, snakefile_text, smk_files)
    return _factory


@pytest.fixture
def make_results(tmp_path):
    def _factory(files: list[str]) -> Path:
        return _make_results(tmp_path, files)
    return _factory
