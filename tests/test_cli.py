from typer.testing import CliRunner

from snakeprune.cli import app

runner = CliRunner()


def test_cli_scan_reports_orphans(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    assert "obsolete.csv" in result.stdout
    # Live files not listed by default
    assert "1.txt" not in result.stdout


def test_cli_scan_dry_run_does_not_delete(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert (results / "obsolete.csv").exists()


def test_cli_scan_delete_flag_unlinks(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "--delete"])
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()


def test_cli_scan_no_snakefile_fails_cleanly(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    result = runner.invoke(app, ["scan", str(empty), str(results)])
    assert result.exit_code != 0
    assert "Snakefile" in result.stdout or "Snakefile" in (result.stderr or "")
