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


def test_cli_scan_recognises_log_files_as_live(make_pipeline, make_results):
    # A rule's log: file lives under the scanned results dir. Because the rule
    # produces it, it must be recognised as live, not reported as an orphan.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}.txt'\n"
        "    log: 'results/logs/{sample}.log'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "logs/1.log"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    assert "1.log" not in result.stdout


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
    assert "Loaded" in combined and "rule path pattern" in combined
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
    assert "0 path patterns" in combined
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


def test_cli_scan_extractor_failure_exits_4(make_pipeline, monkeypatch):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # Force the "python not on PATH" branch via the patterns module.
    monkeypatch.setattr("snakeprune.patterns.shutil.which", lambda _name: None)
    results = pipeline.parent / "results"
    results.mkdir()
    (results / "a.txt").touch()
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 4
    combined = result.stdout + (result.stderr or "")
    assert "PATH" in combined


def test_cli_scan_forwards_configfile_to_extractor(make_pipeline, tmp_path):
    # Rule is gated by config["do_qc"] which defaults to False. Without
    # --configfile, the rule is absent and its expected output file looks
    # like an orphan. With --configfile do_qc.yaml (sets do_qc: true),
    # the rule is present and the output is recognised as live.
    pipeline = make_pipeline(
        "if config.get('do_qc', False):\n"
        "    rule qc:\n"
        "        output: 'results/qc/{sample}.tsv'\n"
        "        shell: 'touch {output}'\n"
        "\n"
        "rule align:\n"
        "    output: 'results/align/{sample}.bam'\n"
        "    shell: 'touch {output}'\n"
    )
    configfile = pipeline / "do_qc.yaml"
    configfile.write_text("do_qc: true\n")
    results = pipeline.parent / "results"
    results.mkdir()
    (results / "qc").mkdir()
    (results / "qc" / "s1.tsv").touch()
    (results / "align").mkdir()
    (results / "align" / "s1.bam").touch()

    # Without configfile: qc rule is hidden, qc output flagged as orphan.
    result_no_cfg = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--allow-high-orphan-rate"]
    )
    assert result_no_cfg.exit_code == 0
    assert "qc/s1.tsv" in result_no_cfg.stdout

    # With configfile: qc rule visible, output recognised as live.
    result_with_cfg = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--configfile", str(configfile)]
    )
    assert result_with_cfg.exit_code == 0
    assert "qc/s1.tsv" not in result_with_cfg.stdout


def test_cli_scan_naughty_dir_dry_run_warns_but_proceeds(make_pipeline, tmp_path):
    # Rule writes under `resources/` so the basename-mismatch refusal does NOT
    # fire; this isolates the naughty-dir warning on a clean dry-run.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'resources/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "obsolete.csv").write_text("x")
    result = runner.invoke(app, ["scan", str(pipeline), str(resources)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "resources" in combined
    assert "input/config directory" in combined
    # Dry-run still lists the orphan.
    assert "obsolete.csv" in result.stdout


def test_cli_scan_naughty_dir_delete_refuses(make_pipeline, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "obsolete.csv").write_text("x")
    result = runner.invoke(
        app, ["scan", str(pipeline), str(config), "--delete", "--yes"]
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "--allow-naughty-dir" in combined
    # Refusal happens before the workflow is even loaded.
    assert "Loading Snakemake workflow" not in combined
    # Nothing deleted.
    assert (config / "obsolete.csv").exists()


def test_cli_scan_naughty_dir_delete_allowed_proceeds(make_pipeline, tmp_path):
    # Rule writes under `config/` so basename-mismatch does not interfere;
    # --allow-naughty-dir lets the delete flow run.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'config/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "obsolete.csv").write_text("x")
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(config),
            "--delete", "--yes", "--allow-naughty-dir", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 0
    assert not (config / "obsolete.csv").exists()


def test_cli_scan_naughty_dir_custom_via_flag(make_pipeline, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    custom = tmp_path / "custom_inputs"
    custom.mkdir()
    (custom / "obsolete.csv").write_text("x")
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(custom), "--delete", "--yes", "--naughty-dir", "custom_inputs"],
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "custom_inputs" in combined
    assert "--allow-naughty-dir" in combined


def test_cli_scan_non_naughty_dir_no_warning(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obsolete.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "input/config directory" not in combined


def test_cli_scan_exclude_dir_relative_prunes_subtree(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    # 'keep/old.csv' would be an orphan, but is excluded; '1.txt' is live.
    results = make_results(["1.txt", "keep/old.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--exclude-dir", "keep"]
    )
    assert result.exit_code == 0
    assert "old.csv" not in result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "Excluded 1 directory subtree" in combined


def test_cli_scan_exclude_dir_absolute_path(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "keep/old.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--exclude-dir", str(results / "keep")],
    )
    assert result.exit_code == 0
    assert "old.csv" not in result.stdout


def test_cli_scan_delete_preserves_live_files(make_pipeline, make_results):
    # The false-positive catastrophe: with live and orphan files side by side,
    # --delete must remove only the orphan and leave every live file intact.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "2.txt", "obsolete.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 0
    assert not (results / "obsolete.csv").exists()
    assert (results / "1.txt").exists()
    assert (results / "2.txt").exists()


def test_cli_scan_delete_removes_exactly_the_reported_orphans(
    make_pipeline, make_results
):
    # Strong invariant across a multi-rule, nested tree: the files removed by
    # --delete are exactly the files the dry-run reported — no live file is
    # touched, and no orphan survives.
    pipeline = make_pipeline(
        "rule align:\n"
        "    output: 'results/align/{sample}.bam'\n"
        "    shell: 'touch {output}'\n"
        "\n"
        "rule call:\n"
        "    output: 'results/call/{sample}.vcf'\n"
        "    shell: 'touch {output}'\n"
    )
    live = ["align/s1.bam", "align/s2.bam", "call/s1.vcf"]
    orphans = ["align/old.tmp", "call/stale.txt", "misc/junk.dat"]
    results = make_results(live + orphans)

    # Dry-run: the report lists every orphan and no live file.
    dry = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert dry.exit_code == 0
    dry_out = dry.stdout + (dry.stderr or "")
    for rel in orphans:
        assert str(results / rel) in dry_out
    for rel in live:
        assert str(results / rel) not in dry_out

    # Delete: the surviving tree is exactly the live set.
    deleted = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--yes", "--allow-high-orphan-rate"],
    )
    assert deleted.exit_code == 0
    remaining = {
        p.relative_to(results).as_posix()
        for p in results.rglob("*")
        if p.is_file()
    }
    assert remaining == set(live)


def test_cli_scan_symlink_orphan_delete_refuses_cleanly(
    make_pipeline, make_results, tmp_path
):
    # --follow-symlinks surfaces a symlinked file as an orphan; without
    # --allow-symlinks the delete must refuse with a clean exit 3 and a message
    # (not crash with a traceback), and must delete nothing.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])  # live
    target = tmp_path / "outside.txt"
    target.write_text("x")
    (results / "stale_link.csv").symlink_to(target)
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(results),
            "--follow-symlinks", "--delete", "--yes", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "--allow-symlinks" in combined
    # Nothing deleted: the symlink and the live file both remain.
    assert (results / "stale_link.csv").is_symlink()
    assert (results / "1.txt").exists()
    assert target.exists()


def test_cli_scan_limit_with_delete_refuses(make_pipeline, make_results):
    # --limit stops the scan early, so the orphan set is partial. Combining it
    # with --delete would delete an arbitrary subset against a truncated
    # orphan-rate denominator; the tool must refuse instead.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv", "obsolete2.csv"])
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(results),
            "--limit", "1", "--delete", "--yes", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "--limit" in combined
    # Nothing deleted: both orphans remain.
    assert (results / "obsolete.csv").exists()
    assert (results / "obsolete2.csv").exists()


def test_cli_scan_limit_with_trash_refuses(make_pipeline, make_results, tmp_path):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv", "obsolete2.csv"])
    trash = tmp_path / "trash"
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(results),
            "--limit", "1", "--trash", str(trash), "--yes", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "--limit" in combined
    assert (results / "obsolete.csv").exists()
    assert (results / "obsolete2.csv").exists()
    assert not trash.exists()


def test_cli_scan_limit_without_delete_is_allowed(make_pipeline, make_results):
    # --limit is a benchmarking aid for dry-run scans; on its own it stays legal.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obsolete.csv"])
    result = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--limit", "1"]
    )
    assert result.exit_code == 0


def test_cli_scan_trash_collision_refuses(make_pipeline, make_results, tmp_path):
    # A prior run already trashed a file at the same relative path. The new run
    # must refuse cleanly (exit 3) rather than clobber the earlier trashed copy.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["obsolete.csv"])
    (results / "obsolete.csv").write_text("new")
    trash = tmp_path / "trash"
    prior = trash / results.name / "obsolete.csv"
    prior.parent.mkdir(parents=True)
    prior.write_text("old")
    result = runner.invoke(
        app,
        [
            "scan", str(pipeline), str(results),
            "--trash", str(trash), "--yes", "--allow-high-orphan-rate",
        ],
    )
    assert result.exit_code == 3
    combined = result.stdout + (result.stderr or "")
    assert "already exists" in combined
    # Source orphan untouched; prior trashed copy intact.
    assert (results / "obsolete.csv").read_text() == "new"
    assert prior.read_text() == "old"


def test_cli_scan_delete_with_zero_orphans_is_clean_noop(make_pipeline, make_results):
    # Everything is live. --delete (even without --yes) must skip the prompt /
    # non-TTY refusal entirely and exit 0 with nothing removed.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "2.txt"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "--delete"])
    assert result.exit_code == 0
    assert (results / "1.txt").exists()
    assert (results / "2.txt").exists()


def test_cli_scan_delete_is_idempotent_on_rerun(make_pipeline, make_results):
    # Deleting twice is safe: the second pass finds zero orphans and exits clean.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "obsolete.csv"])
    first = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--yes", "--allow-high-orphan-rate"],
    )
    assert first.exit_code == 0
    assert not (results / "obsolete.csv").exists()
    assert (results / "1.txt").exists()

    second = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--yes", "--allow-high-orphan-rate"],
    )
    assert second.exit_code == 0
    assert (results / "1.txt").exists()
    combined = second.stdout + (second.stderr or "")
    assert "found 0 orphan" in combined.lower()


def test_cli_scan_brace_quantifier_constraint_is_live(make_pipeline, make_results):
    # Snakemake inlines global and rule-level wildcard_constraints into every
    # output string as `{name,regex}`. A regex with a brace quantifier such as
    # `[0-9]{1,2}` (chromosomes!) must be parsed, not escaped into a literal
    # that matches nothing — otherwise every file of the rule is an orphan and
    # --delete would remove live outputs.
    pipeline = make_pipeline(
        "wildcard_constraints:\n"
        "    chrom = '[0-9]{1,2}'\n"
        "\n"
        "rule a:\n"
        "    output: 'results/{chrom}.g.txt'\n"
        "    shell: 'touch {output}'\n"
        "\n"
        "rule b:\n"
        "    wildcard_constraints: sample = '[0-9]{2}'\n"
        "    output: 'results/{sample}.r.txt'\n"
        "    shell: 'touch {output}'\n"
        "\n"
        "rule c:\n"
        "    output: 'results/{sample,[a-z]{3}}.i.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["7.g.txt", "12.r.txt", "abc.i.txt", "orphan.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0, result.output
    assert "orphan.csv" in result.stdout
    assert "7.g.txt" not in result.stdout
    assert "12.r.txt" not in result.stdout
    assert "abc.i.txt" not in result.stdout


def test_cli_scan_unparseable_pattern_exits_4(make_pipeline, make_results, monkeypatch):
    # If the extractor ever hands back a brace sequence the parser does not
    # understand, the scan must stop with exit 4 — never escape it into a
    # literal that flags every file of the rule as an orphan.
    from snakeprune.patterns import RuleSpec
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    monkeypatch.setattr(
        "snakeprune.patterns.load_rule_specs",
        lambda *a, **k: [RuleSpec(name="a", outputs=["results/{bad name}.txt"], constraints={})],
    )
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 4
    assert "Cannot parse wildcard syntax" in result.output
    assert "1.txt" not in result.stdout


def test_cli_scan_files_inside_directory_output_are_live(make_pipeline, make_results):
    # A directory() output is produced as a whole; every file inside — including
    # Snakemake's own .snakemake_timestamp marker — is live. Deleting them would
    # corrupt an output Snakemake still believes is complete.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: directory('results/{sample}/outdir')\n"
        "    shell: 'mkdir -p {output}'\n"
        "\n"
        "rule b:\n"
        "    output: directory('results/static')\n"
        "    shell: 'mkdir -p {output}'\n"
    )
    results = make_results(
        [
            "A/outdir/x.txt",
            "A/outdir/deep/y.txt",
            "A/outdir/.snakemake_timestamp",
            "static/z.txt",
            "A/other.txt",
        ]
    )
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0, result.output
    assert "other.txt" in result.stdout
    assert "x.txt" not in result.stdout
    assert "y.txt" not in result.stdout
    assert ".snakemake_timestamp" not in result.stdout
    assert "z.txt" not in result.stdout


def test_cli_scan_permissive_wildcards_matches_slash_in_wildcard(make_pipeline, make_results):
    # Snakemake's own default wildcard regex is `.+`, which spans `/`. A file
    # Snakemake produced with sample='x/y' is live under --permissive-wildcards
    # (and, deliberately, an orphan under the stricter default — see README).
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{sample}/flat.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["x/y/flat.txt", "x/flat.txt"])
    strict = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert strict.exit_code == 0, strict.output
    assert "x/y/flat.txt" in strict.stdout
    assert "x/flat.txt" not in strict.stdout
    permissive = runner.invoke(
        app, ["scan", str(pipeline), str(results), "--permissive-wildcards"]
    )
    assert permissive.exit_code == 0, permissive.output
    assert "flat.txt" not in permissive.stdout


def test_cli_scan_absolute_output_under_project_is_live(make_pipeline, make_results):
    pipeline = make_pipeline(
        "import os\n"
        "OUT = os.path.abspath('results')\n"
        "rule a:\n"
        "    output: OUT + '/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "orphan.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0, result.output
    assert "orphan.csv" in result.stdout
    assert "1.txt" not in result.stdout


def test_cli_scan_foreign_absolute_output_warns_even_when_quiet(make_pipeline, make_results):
    # Dropping a pattern narrows the live set, which is safety-relevant, so the
    # warning is not subject to --quiet.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: '/nowhere/elsewhere/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
        "rule b:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results), "--quiet"])
    assert result.exit_code == 0, result.output
    assert "/nowhere/elsewhere/{n}.txt" in result.output
    assert "WARNING" in result.output


def test_cli_scan_all_rules_foreign_absolute_hits_empty_rule_refusal(make_pipeline, make_results):
    # After dropping every absolute pattern nothing is left; the existing
    # empty-rule refusal (exit 3) must fire rather than scanning with no
    # patterns and reporting every file as an orphan.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: '/nowhere/elsewhere/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 3
    assert "Refusing to scan" in result.output


def test_cli_scan_delete_partial_failure_exits_5_with_count(
    make_pipeline, make_results, monkeypatch
):
    # Safety: when deletion fails mid-batch some files are already gone, so the
    # CLI must report the real count and exit 5, never claim it "refused".
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "o1.csv", "o2.csv", "o3.csv"])
    from pathlib import Path as _P
    real_unlink = _P.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(f"[Errno 13] Permission denied: '{self}'")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(_P, "unlink", flaky_unlink)
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--delete", "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 5
    assert "Deleted 1 of 3" in result.output
    assert "Refusing" not in result.output
    assert (results / "1.txt").exists()


def test_cli_scan_trash_inside_results_dir_refuses(make_pipeline, make_results):
    # Trashing into the scanned tree means the next scan reports the trash as
    # orphans and --delete would then unlink the only copy.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--trash", str(results / ".trash"), "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 3
    assert "Refusing" in result.output
    assert "--trash" in result.output
    assert (results / "orphan.csv").exists()


def test_cli_scan_trash_equal_to_results_dir_refuses(make_pipeline, make_results):
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--trash", str(results), "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 3
    assert "--trash" in result.output
    assert (results / "orphan.csv").exists()


def test_cli_scan_trash_as_ancestor_of_results_dir_collides_and_refuses(
    make_pipeline, make_results, tmp_path
):
    # --trash pointing at the *parent* of results_dir makes the trash target
    # `<parent>/results/<rel>` == the source file. The existing collision guard
    # refuses before anything moves. Pinned so a future refactor of the guard
    # cannot silently allow a self-move.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--trash", str(tmp_path), "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 3
    assert "already exists" in result.output
    assert (results / "orphan.csv").exists()


def test_cli_scan_trash_via_symlinked_results_dir_refuses(make_pipeline, tmp_path):
    # Safety: results is a symlink to /real; --trash /real/.trash is inside the
    # scanned tree by realpath even though the abspath comparison passes.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    real = tmp_path / "real"
    real.mkdir()
    (real / "orphan.csv").touch()
    link = tmp_path / "results"
    link.symlink_to(real)
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(link), "--trash", str(real / ".trash"), "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 3, result.output
    assert "--trash" in result.output
    assert (real / "orphan.csv").exists()


def test_cli_scan_trash_sibling_dir_with_results_prefix_is_allowed(
    make_pipeline, make_results, tmp_path
):
    # Safety/usability: a sibling whose name merely starts with `results` is
    # outside the tree; the guard must compare on a path-separator boundary.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    trash = tmp_path / "results_trash"
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--trash", str(trash), "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 0, result.output
    assert (trash / "results" / "orphan.csv").exists()
    assert not (results / "orphan.csv").exists()


def test_cli_scan_trash_relative_path_inside_results_refuses(
    make_pipeline, make_results, monkeypatch
):
    # Safety: a relative --trash resolves against the cwd; from inside results
    # it lands in the scanned tree.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["orphan.csv"])
    monkeypatch.chdir(results)
    result = runner.invoke(
        app,
        ["scan", str(pipeline), str(results), "--trash", ".trash", "--yes", "--allow-high-orphan-rate"],
    )
    assert result.exit_code == 3, result.output
    assert "--trash" in result.output
    assert (results / "orphan.csv").exists()


def test_cli_scan_invalid_constraint_regex_exits_4(make_pipeline, make_results):
    # Safety: a bad constraint regex must be a clean exit-4 pattern error, not
    # a traceback, and must never fall through to listing orphans.
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: 'results/{x,[a-z}/a.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["q/a.txt", "orphan.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 4, result.output
    assert "Invalid wildcard constraint" in result.output
    assert "orphan.csv" not in result.output


def test_cli_scan_absolute_output_under_results_dir_is_live(
    make_pipeline, make_results, tmp_path
):
    # Safety: a rule writing to an absolute path under the scanned results dir
    # (but outside the workdir) must keep its files live, not orphan them.
    results_abs = str(tmp_path / "results")
    pipeline = make_pipeline(
        "rule a:\n"
        "    output: '" + results_abs + "/{n}.txt'\n"
        "    shell: 'touch {output}'\n"
    )
    results = make_results(["1.txt", "orphan.csv"])
    result = runner.invoke(app, ["scan", str(pipeline), str(results)])
    assert result.exit_code == 0, result.output
    assert "orphan.csv" in result.stdout
    assert "1.txt" not in result.stdout
    assert "WARNING: dropping" not in result.output
