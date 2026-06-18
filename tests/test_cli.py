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
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()


def test_cli_scan_no_snakefile_fails_cleanly(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    result = runner.invoke(app, ["scan", str(empty), str(results)])
    assert result.exit_code == 2
    assert "Snakefile" in result.stdout or "Snakefile" in (result.stderr or "")


def test_cli_scan_emits_progress_by_default(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Loading Snakemake workflow" in combined
    assert "Loaded" in combined and "rule output pattern" in combined
    assert "Walking" in combined
    assert "Scanned" in combined and "orphan" in combined


def test_cli_scan_quiet_suppresses_progress(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "--quiet"])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Loading" not in combined
    assert "Walking" not in combined
    assert "Scanned" not in combined
    # Orphan listing still emitted
    assert "obsolete.csv" in result.stdout


def test_cli_scan_short_q_flag_also_suppresses(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "-q"])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Loading" not in combined
    assert "Walking" not in combined


def test_cli_scan_refuses_when_workflow_has_no_rules(tmp_path):
    # A workflow that defines no rules (just a configfile statement is enough
    # to make Snakemake load successfully but produce 0 rules).
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / "Snakefile").write_text("# no rules here\n")
    results = tmp_path / "results"
    results.mkdir()
    (results / "anything.txt").write_text("x")
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "0 output patterns" in combined
    assert "--allow-empty-rules" in combined


def test_cli_scan_allow_empty_rules_bypasses_refusal(tmp_path):
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / "Snakefile").write_text("# no rules here\n")
    results = tmp_path / "results"
    results.mkdir()
    (results / "anything.txt").write_text("x")
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--allow-empty-rules"]
    )
    assert result.exit_code == 0
    assert "anything.txt" in result.stdout


def test_cli_scan_refuses_when_no_rule_writes_under_results_dir_basename(
    make_pipeline, tmp_path
):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # User points at a directory whose basename ('outputs') doesn't appear in
    # any rule's literal prefix (rules write under 'results/').
    wrong = tmp_path / "outputs"
    wrong.mkdir()
    (wrong / "x.txt").write_text("x")
    result = runner.invoke(app, ["scan", str(pipeline), str(wrong)])
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "outputs/" in combined
    assert "results/" in combined  # surfaced as the actual prefix
    assert "--allow-basename-mismatch" in combined


def test_cli_scan_allow_basename_mismatch_bypasses_refusal(make_pipeline, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    wrong = tmp_path / "outputs"
    wrong.mkdir()
    (wrong / "x.txt").write_text("x")
    result = runner.invoke(
        app, ["scan", str(pipeline), str(wrong), "--allow-basename-mismatch"]
    )
    assert result.exit_code == 0
    assert "x.txt" in result.stdout


def test_cli_scan_warns_when_orphan_rate_high(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 1 live ('1.txt') vs 3 orphans -> 75% rate.
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "WARNING" in combined
    assert "75" in combined  # percentage in the warning


def test_cli_scan_no_warning_below_threshold(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 3 live vs 1 orphan -> 25% rate, well below default 50% threshold.
    results = make_results(["1.txt", "2.txt", "3.txt", "obs.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "WARNING" not in combined


def test_cli_scan_empty_results_dir_no_warning_no_crash(make_pipeline, make_results):
    # An empty results dir means file_count == 0; the high-orphan-rate block
    # must skip cleanly (no ZeroDivisionError, no warning).
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results([])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "WARNING" not in combined
    assert "ZeroDivisionError" not in combined


def test_cli_scan_threshold_flag_disables_check_at_one(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--orphan-rate-threshold", "1.0"]
    )
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "WARNING" not in combined


def test_cli_scan_delete_non_tty_without_yes_refuses(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "--yes" in combined
    # Nothing deleted
    assert (results / "obsolete.csv").exists()


def test_cli_scan_delete_high_rate_refused_without_allow_flag(
    make_pipeline, make_results
):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 75% orphan rate.
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--delete", "--yes"]
    )
    assert result.exit_code == 3
    # All orphans preserved.
    assert (results / "obs1.csv").exists()
    combined = result.stdout + (result.stderr or "")
    assert "--allow-high-orphan-rate" in combined


def test_cli_scan_delete_high_rate_proceeds_with_allow_flag(
    make_pipeline, make_results
):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obs1.csv", "obs2.csv", "obs3.csv"])
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(results),
            "--delete", "--yes", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 0
    assert not (results / "obs1.csv").exists()


def test_cli_scan_delete_prompt_aborts_on_n(make_pipeline, make_results, monkeypatch):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    # Simulate a TTY so the prompt branch is taken.
    monkeypatch.setattr("snakeprune.cli._stdin_isatty", lambda: True)
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--allow-high-orphan-rate"],
        input="n\n",
    )
    assert result.exit_code == 0
    assert (results / "obsolete.csv").exists()
    combined = result.stdout + (result.stderr or "")
    assert "Aborted" in combined


def test_cli_scan_delete_prompt_proceeds_on_y(make_pipeline, make_results, monkeypatch):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    monkeypatch.setattr("snakeprune.cli._stdin_isatty", lambda: True)
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--allow-high-orphan-rate"],
        input="y\n",
    )
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()


def test_cli_scan_surfaces_skipped_symlinked_dirs(make_pipeline, make_results, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    external = tmp_path / "external"
    external.mkdir()
    (external / "x.txt").write_text("x")
    (results / "link_dir").symlink_to(external)
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "Skipped 1 symlinked subdirectory" in combined


def test_cli_scan_trash_moves_orphan_to_dir(make_pipeline, make_results, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    trash = tmp_path / "trash"
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--trash", str(trash), "--yes", "--allow-high-orphan-rate"]
    )
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()
    assert (trash / results.name / "obsolete.csv").exists()


def test_cli_scan_trash_implies_delete_mode(make_pipeline, make_results, tmp_path):
    # User passes --trash but not --delete; deletion should still happen.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    trash = tmp_path / "trash"
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--trash", str(trash), "--yes", "--allow-high-orphan-rate"]
    )
    assert result.exit_code == 0
    assert (trash / results.name / "obsolete.csv").exists()
