# snakeprune test coverage: what we check and why you can trust it

This document is written to be read, not skimmed. Its purpose is to let you (or
a colleague who is rightly suspicious of a tool that deletes files) understand
exactly what the test suite guarantees and where its edges are. It is honest
about what is *not* yet covered, because a coverage report that claims
perfection is less trustworthy than one that names its gaps.

As of this writing the suite is **226 tests**, split across four files:

- `tests/test_patterns.py` (82) — does snakeprune correctly understand what a
  Snakemake workflow actually produces?
- `tests/test_walker.py` (24) — does it find the right files on disk?
- `tests/test_delete.py` (18) — does the deletion primitive refuse the things
  it should and only remove the things it should?
- `tests/test_cli.py` (102) — does the whole command-line tool, end to end,
  behave safely under every flag combination that matters?

Run them all with `python -m pytest` from the project root. They use a real
Snakemake (the extractor runs as a subprocess), synthetic pipelines, and real
temporary directories — there is almost no mocking, so a passing test means the
real code path ran on a real filesystem.

## The one question that matters

A tool that deletes files earns trust by answering a single question
convincingly:

> **Can it ever delete a file that is actually a live pipeline output, and when
> it does delete, does it remove *only* the files it told you it would?**

Everything below is organised around that question. snakeprune defends the
answer in two parts: first by being *correct* about what counts as an orphan,
and second by layering *refusals* in front of every destructive action so that
a wrong answer cannot quietly turn into data loss.

## Part 1 — Being correct about what is an orphan

An "orphan" is a file under your results directory that no rule in your
workflow could have produced. snakeprune does not guess this from filenames; it
loads your actual Snakemake workflow and asks it what its rules output.

**It reads the real workflow, not a parody of it.** `tests/test_patterns.py`
runs a standalone extractor script as a subprocess against synthetic Snakefiles
and checks that the path patterns it recovers match what Snakemake itself
would resolve. This includes the cases that are easy to get wrong:

- Wildcard constraints, whether declared globally
  (`wildcard_constraints:`), inline (`{x,[0-9]+}`), or rule-local — and the
  precedence between them (rule-local wins). A constraint like `\d+` means
  `results/123.txt` is recognised as live but `results/abc.txt` is not, so the
  constraint genuinely narrows what is considered "produced".
- `multiext(...)` outputs, which expand to several patterns from one
  declaration.
- Rules imported from other files via `module ... use rule * from ...`,
  including the `prefix:` modifier that rewrites their output paths. Without
  this, every file under a prefixed subdirectory would look like an orphan —
  exactly the kind of mass false-positive that would be catastrophic under
  `--delete`. There is a dedicated test for it.
- Repeated wildcards (`results/{s}/{s}.bam`) preserve Snakemake's
  implicit-equality rule via a regex backreference, so `results/a/a.bam`
  matches but `results/a/b.bam` does not.

**It translates those patterns into matchers faithfully.** The conversion from
a Snakemake rule path pattern to an anchored regex escapes literal regex
metacharacters (dots, plus signs, brackets) so they are matched literally, and
the combiner that merges every rule's pattern into one matcher is tested to
preserve the match semantics of the individual patterns — including when
different rules reuse the same wildcard name, which a naive merge would crash
on.

**It fails loudly instead of guessing.** If the workflow environment is wrong,
snakeprune refuses rather than producing a bogus orphan list. The tests cover:
the Snakefile being missing (clean exit, not a crash), Python not being on the
PATH, Snakemake not being importable in that interpreter, and the extractor
emitting unparseable output. Each produces a specific, actionable error message
and a distinct exit code rather than a silent empty pattern set.

**It walks the filesystem deterministically.** `tests/test_walker.py` checks
that the directory walk returns regular files only (never directories), honours
`--ignore` glob patterns, and — importantly for safety — **skips symlinks by
default**. A symlink is only ever visited if you explicitly pass
`--follow-symlinks`. Symlinked subdirectories are never descended into, and the
walker counts and surfaces how many it skipped so a silently-unscanned subtree
cannot hide from you. The `--exclude-dir` option prunes whole subtrees from the
scan, and that pruning is tested for relative paths, absolute paths,
the reported tally, and the refusals (exit 3) for values that do not exist,
are files, are symlinks, lie outside the results directory, or are the results
directory itself (`test_cli_exclude_dir_*`). `--ignore` glob semantics are
parametrised (`test_ignore_glob_semantics`): `*` crosses `/`, and `**/` also
matches zero directories, so `**/*.bai` covers top-level files. Symlinked
directories are counted and reported with and without `--follow-symlinks`
(`test_iter_results_files_counts_skipped_symlinked_dirs_when_following`,
`test_cli_follow_symlinks_reports_skipped_symlinked_dirs`).

### Rule-path normalisation, nested results directories and extractor stdout

Round 2 of review fixes closed several more silent-false-orphan paths:

- Rule paths with `./`, `//`, a trailing `/` or `seg/..` are normalised
  (`test_normalise_rule_path`, `test_cli_scan_unnormalised_rule_paths_are_live`);
  a leading `..` resolves against the workdir, and a `..` after a wildcard
  segment is refused with exit 4
  (`test_find_rule_patterns_refuses_dotdot_after_wildcard`,
  `test_cli_scan_dotdot_after_wildcard_exits_4`).
- Scanned files are matched by the results directory's path relative to the
  workflow workdir, including via a symlinked results dir, a results dir equal
  to the workdir, and the basename fallback (`test_results_match_prefix`,
  `test_cli_scan_nested_results_dir_uses_workdir_relative_path`,
  `test_cli_scan_results_dir_via_symlink_matches`,
  `test_cli_scan_results_dir_equal_to_workdir`,
  `test_cli_basename_guard_uses_match_prefix`).
- Constraints with numeric backreferences stay correct when rule patterns are
  combined (`test_combine_rule_patterns_*backrefs*`,
  `test_cli_scan_constraint_with_numeric_backref`).
- A Snakefile that prints, including from a child process, no longer corrupts
  the extractor's JSON (`test_run_extractor_tolerates_print_in_snakefile`,
  `test_run_extractor_tolerates_child_process_stdout`,
  `test_cli_scan_with_print_in_snakefile`).
- A literal prefix is no longer cut at an escaped `\(`, and rule prefixes are
  computed once per scan (`test_extract_literal_prefix_*escaped_paren*`,
  `test_attribute_orphan_uses_precomputed_prefixes`).

### Final-review fixes

- Scanning the workflow directory itself may only be a dry run: `--delete` and
  `--trash` are refused (exit 3) unless `--allow-basename-mismatch` is passed
  (`test_cli_delete_refused_when_results_dir_is_workdir`,
  `test_cli_delete_workdir_allowed_with_override`).
- A file is live if it matches under the workdir-relative prefix *or* the
  results-dir basename, so running `snakemake` from a directory snakeprune does
  not know about does not orphan relative outputs
  (`test_cli_scan_workdir_directive_with_absolute_rule_keeps_relative_outputs`).
- An orphan that matches a rule only if an unconstrained wildcard spans `/` is
  reported on stderr (never stdout) and held back from `--delete` and `--trash` unless
  `--delete-ambiguous` is passed; an explicitly constrained wildcard is never
  ambiguous (`test_cli_scan_ambiguous_orphan_is_listed_but_not_deleted`,
  `test_cli_scan_only_ambiguous_orphans_deletes_nothing`,
  `test_cli_scan_delete_ambiguous_flag_deletes_them`,
  `test_cli_scan_ambiguous_orphan_is_held_back_from_trash`,
  `test_cli_scan_constrained_wildcard_orphan_is_not_ambiguous`).
- Deletion is refused while `.snakemake/locks` in the working directory is
  non-empty, unless `--allow-locked`; an empty locks directory does not block
  (`test_cli_scan_delete_refuses_while_snakemake_locks_exist`,
  `test_cli_scan_empty_locks_directory_does_not_block`,
  `test_cli_scan_locks_are_checked_in_the_directory_option_workdir`).
- Deletion is refused for the filesystem root
  (`test_cli_scan_delete_refuses_filesystem_root`).
- A `workdir:` directive is read (never created or entered) and `--directory`
  overrides it; nested results directories and leading `..` paths resolve
  against it (`test_cli_scan_honours_workdir_directive_for_nested_results`,
  `test_cli_scan_workdir_directive_resolves_leading_dotdot`,
  `test_cli_scan_workdir_directive_is_not_created`,
  `test_cli_scan_directory_option_sets_workdir`).
- Names stored decomposed on disk match composed rule text and `--ignore` globs
  (`test_cli_scan_matches_decomposed_unicode_names_on_disk`,
  `test_cli_scan_ignore_glob_matches_decomposed_unicode_names`), including
  when the results directory's own name is the decomposed one
  (`test_cli_scan_results_dir_with_decomposed_name_relative_rule`,
  `test_cli_scan_results_dir_with_decomposed_name_absolute_rule`).
- `--ignore "dir/**"` reads `dir` both as a glob and as literal text, so
  `run[1]/**` protects a directory named `run[1]`
  (`test_cli_scan_ignore_subdir_glob_treats_prefix_literally_too`).
- `--ignore` globs compile to one regex; repeated `**/` collapse and more than
  three separate `**/` are refused
  (`test_ignore_glob_repeated_double_stars_collapse`,
  `test_ignore_glob_with_too_many_double_stars_is_rejected`,
  `test_cli_scan_ignore_glob_with_too_many_double_stars_refuses`).
- `--exclude-dir` must match the on-disk spelling, so `KEEP` for `keep/` is
  refused instead of silently protecting nothing
  (`test_cli_exclude_dir_wrong_case_refuses`).
- `--exclude-dir` reached through a symlinked parent directory is refused, since
  the walk reaches those files by their real path
  (`test_cli_exclude_dir_through_symlinked_parent_refuses`).
- A results directory spelled `.`, `..` or `x/..` is treated by its real name:
  trash targets stay inside the trash directory and the naughty-directory guard
  still fires (`test_cli_scan_trash_stays_inside_trash_when_results_dir_ends_in_dotdot`,
  `test_cli_scan_naughty_dir_spelled_as_dot_still_refuses`).
- `--trash` inside the results tree is detected by file identity, so another
  letter case on a case-insensitive filesystem is refused
  (`test_cli_scan_trash_inside_results_via_other_letter_case_refuses`; skipped
  on case-sensitive filesystems).
- A `--trash` path that is itself a file, and a dangling-symlink ancestor inside
  the trash, are refused before anything moves
  (`test_delete_orphans_trash_refuses_when_trash_dir_is_a_file`,
  `test_delete_orphans_trash_refuses_dangling_symlink_ancestor`).

### Constraint syntax, `directory()` outputs and absolute paths

Three classes of input used to produce *silent* false orphans and are now pinned:
brace-quantified constraints (`{chrom,[0-9]{1,2}}`, at inline, rule and global
level — `test_cli_scan_brace_quantifier_constraint_is_live`), files inside a
`directory()` output including `.snakemake_timestamp`
(`test_cli_scan_files_inside_directory_output_are_live`), and outputs built from
an absolute path under the project (`test_cli_scan_absolute_output_under_project_is_live`).
Anything brace-shaped the parser cannot read now raises instead of being
escaped into a never-matching literal (`test_unparseable_brace_raises_pattern_error`,
`test_cli_scan_unparseable_pattern_exits_4`). The unused `walker.find_orphans`
entry point was removed, along with its 5 tests.

## Part 2 — Layered refusals in front of every deletion

Correctness can still be wrong — you might point the tool at the wrong
directory, or load the wrong config. So snakeprune treats deletion as
guilty-until-proven-safe. Each of the following guards is individually tested,
both that it fires when it should and that its override flag works when you
genuinely mean it.

1. **Dry-run by default.** Nothing is deleted unless you pass `--delete` (or
   `--trash`). The default run only lists. Tested: a plain scan leaves every
   file on disk.

2. **The "naughty directory" guard.** If you point at a directory whose name is
   a conventional Snakemake *input/config* location — `resources`, `config`,
   `profile`, `workflow`, `.snakemake`, or any name you add with
   `--naughty-dir` — snakeprune warns on a dry-run and *refuses to delete*
   (exit 3) unless you pass `--allow-naughty-dir`. The refusal happens before
   the workflow is even loaded, so a fat-fingered `config` target can't get
   anywhere near a deletion.

3. **The empty-rules guard.** If the workflow loads but yields zero output
   patterns, every file would be reported as an orphan. snakeprune refuses to
   even scan in that state unless you pass `--allow-empty-rules`.

4. **The basename-mismatch guard.** If no rule writes under the directory you
   targeted (matched by its path relative to the workflow workdir or by its
   basename), that usually means you pointed at the wrong place.
   snakeprune refuses and tells you which prefixes the rules *do* write under,
   unless you pass `--allow-basename-mismatch`.

5. **The high-orphan-rate guard.** If more than a threshold fraction (default
   50%) of scanned files come back as orphans, that is a red flag for a config
   or environment problem rather than real cleanup. snakeprune warns always and
   *refuses to delete* unless you pass `--allow-high-orphan-rate`. The threshold
   is configurable, the empty-directory case is handled without a divide-by-zero,
   and setting the threshold to 1.0 disables it — all tested.

6. **Confirmation before deleting.** In an interactive terminal you get a Y/N
   prompt; answering anything but yes aborts with nothing deleted. In a
   non-interactive context (a script, a pipe) snakeprune *refuses* unless you
   explicitly pass `--yes`, so it can never be surprised into a silent deletion
   by a cron job. Both the abort-on-no and proceed-on-yes paths are tested, as
   is the non-TTY refusal.

7. **Symlink and directory refusals at the point of deletion.** The deletion
   primitive itself refuses to remove a directory, and refuses to remove a
   symlink unless `--allow-symlinks` is set (and when it does remove a symlink,
   it removes the link, never the target — tested explicitly).

8. **All-or-nothing batch validation (the most recent hardening).** This is the
   one that closed a genuine data-loss bug. Deletion now validates *every*
   file in the batch against the directory and symlink guards *before removing
   anything*. Previously the tool deleted file by file, so a disallowed entry
   in the middle of the list would raise — but only after the valid files
   listed before it had already been unlinked. There is now a test that puts a
   good file ahead of a forbidden symlink and asserts the good file still
   exists after the refusal. At the CLI level, that refusal is turned into a
   clean `Refusing to delete: …` message and exit code 3, instead of an
   uncaught error and a stack trace.

9. **`--limit` cannot be combined with deletion.** `--limit` is a benchmarking
   aid that truncates the scan after N files, which makes the orphan set partial
   and the orphan-rate guard meaningless. Passing it together with `--delete` or
   `--trash` is refused outright (exit 3) before anything is scanned, so it can
   never delete an arbitrary subset of a half-finished scan. On its own, for a
   dry run, it stays legal. All three cases are tested.

10. **`--trash` as a reversible alternative, and it won't clobber itself.**
   Instead of unlinking, you can move orphans into a trash directory; the
   original relative structure is preserved under
   `<trash>/<results-dir-name>/…`, and the namespacing lets one trash directory
   serve multiple results directories without collisions. If a *previous* run
   already left a file at the same path in the trash, the move is refused (exit
   3) rather than silently overwriting it — otherwise the safety net would quietly
   destroy your earlier recovery copy, which is exactly the failure the trash is
   meant to prevent. The same pre-flight pass refuses when a parent of a target
   exists as a non-directory (`test_delete_orphans_trash_refuses_when_ancestor_is_a_file`,
   `test_cli_trash_ancestor_file_refuses_with_exit_3`; an existing directory
   parent is allowed). Like the deletion guards, this collision check runs in the
   all-or-nothing pre-flight pass, so one colliding orphan aborts the whole batch
   before anything has moved.

11. **`--trash` cannot point into the results tree.** A trash directory equal to,
   or beneath, the scanned results directory is refused (exit 3), since the next
   scan would report the trashed files as orphans
   (`test_cli_scan_trash_inside_results_dir_refuses`,
   `test_cli_scan_trash_equal_to_results_dir_refuses`).

12. **Partial deletion is reported honestly.** If a batch fails part-way,
   snakeprune stops, reports how many files were removed, and exits 5
   (`test_delete_orphans_mid_batch_failure_reports_progress`,
   `test_cli_scan_delete_partial_failure_exits_5_with_count`).

## The headline data-loss invariants

Two tests exist specifically to be the thing you point a sceptic at. They do
not test a clever edge case; they test the boring promise the tool makes.

- **Live files survive a real delete.** With live and orphan files sitting side
  by side in the same directory, an actual `--delete` removes only the orphan
  and leaves every live file in place. (Most other delete tests use a directory
  containing only orphans; this one deliberately mixes them, because the
  false-positive deletion is the catastrophe worth pinning down.)

- **The deleted set equals the reported set.** Over a nested, multi-rule tree,
  the dry-run lists exactly the orphans and no live file; then a real `--delete`
  is run and the *surviving* tree is asserted to equal the live set exactly.
  This proves two things at once: nothing that was reported as live was touched,
  and nothing that was reported as an orphan survived. There are no surprise
  deletions beyond the list you were shown.

Two further tests pin the quiet, boring cases that a careful operator relies on:

- **`--delete` with nothing to delete is a clean no-op.** When every file is
  live, `--delete` skips the confirmation prompt and the non-interactive refusal
  entirely and exits 0 with everything in place — it does not stall waiting for a
  yes, and it does not error.

- **Deleting twice is safe.** A second `--delete` over an already-cleaned tree
  finds zero orphans and exits cleanly, leaving the live files untouched. Re-runs
  don't compound.

## What is NOT yet covered (read this part too)

Trust comes from knowing the edges, so here are the gaps that remain:

- **Case-insensitive filesystems** (e.g. default macOS) and **dotfiles**: the
  matcher is case-sensitive and the walker includes dotfiles; only the
  `--exclude-dir` spelling check has a dedicated test, so the rest is by
  inspection rather than by proof.

None of these are believed to cause silent data loss today, but they are the
honest next targets if you want the suite to be airtight.

## How to convince yourself in five minutes

1. `python -m pytest -q` — expect all green.
2. Open `tests/test_cli.py` and read
   `test_cli_scan_delete_removes_exactly_the_reported_orphans`. It is the whole
   safety argument in one readable function.
3. Open `tests/test_delete.py` and read
   `test_delete_orphans_validates_whole_batch_before_deleting_any`. Temporarily
   break the fix (revert `delete.py` to delete file-by-file) and watch that test
   go red — that is the proof the test is real and not decorative.
