# snakeprune Safety & Pre-Release Hardening — Design

**Date:** 2026-06-17
**Status:** Draft, awaiting user review

## Problem

snakeprune is approaching a public release. Today the tool will happily report every file in a results directory as an orphan and (with `--delete`) unlink them all, in several realistic failure modes the user can't easily detect:

- **Workflow loads incompletely.** Conditional `include:` blocks gated by missing config, missing Python deps, or running in a different env from the one that produced the files → rule list is silently a subset (or empty) of what was used at run time → outputs of missing rules are flagged as orphans.
- **CLI misalignment.** The CLI matches rule patterns against `<results_dir.name>/<rel>`. Pointing at `/scratch/foo/` when rules write to `results/...` makes *every* file an orphan. Same risk if the user picks the wrong pipeline directory.
- **Destructive deletion when either of the above has gone undetected.** `--delete` is irrevocable; there is no confirmation prompt, no preview, and no undo.
- **Silent blind spots.** Symlinked subdirectories are not recursed and their existence is not surfaced — files there can never be reported as orphans even if they are.

This spec adds defensive checks, an interactive deletion flow, visibility into blind spots, and an opt-in trash mode that makes deletion reversible. It also adds matching `Safety` / `Limitations` content to the README so users see the guardrails before they reach for `--delete`.

## Approach

Add safety as layered checks, with each layer individually overridable so power users can bypass any specific guard without losing the others. The CLI runs pre-scan refusals (cheap, no walk required) first, then performs the scan, then runs post-scan checks, then — only if `--delete` or `--trash` is set — the deletion flow, which itself surfaces a summary and confirmation prompt before doing anything destructive.

Trash mode is an opt-in *destination* for the deletion action: passing `--trash DIR` redirects unlinks into moves under `DIR`. Default behaviour (`--delete` alone) is unchanged — straight unlink — so existing users / scripts are not silently changed.

## Components

### Pre-scan refusals (in `cli.scan`)

Two checks, both run after `find_rule_patterns` returns but before the walk starts:

1. **Empty rule list.** If `find_rule_patterns()` returns `[]`, exit with code 3 and message:

   > Workflow loaded but produced 0 output patterns. Refusing to scan: every file would be reported as an orphan. Check that the right config / env is loaded, or pass `--allow-empty-rules` to override.

2. **Results-dir basename does not match any rule prefix.** For each compiled pattern, extract its literal prefix (reuse the logic in `attribute_orphan_to_rule`). If *no* rule's literal prefix starts with `<results_dir.name>/`, exit with code 3 and message:

   > No rule writes under `<results_dir.name>/`. Rules write under: <top-3 most-common literal first-segments>. Did you point at the wrong directory? Pass `--allow-basename-mismatch` to override.

Each check has its own bypass flag — `--allow-empty-rules`, `--allow-basename-mismatch` — so a user who is confident in one situation does not have to relax the other.

### Post-scan checks (in `cli.scan`)

3. **High orphan rate.** After the walk, compute `rate = orphans / scanned`. If `rate > --orphan-rate-threshold` (default 0.5), print a bold warning before the orphan listing:

   > WARNING: <PCT>% of scanned files are orphans (<count> of <total>). This is unusually high and usually indicates a config or environment problem rather than real cleanup. Review the list carefully before deleting.

   Used as a gate in the deletion flow below.

### Deletion flow (in `cli.scan`, behind `--delete` or `--trash DIR`)

Triggered when either `--delete` or `--trash DIR` is set (the latter implies deletion, just with a non-unlink destination).

1. Compute deletion summary: orphan count + total bytes (one extra `stat` per orphan; orphans are typically rare so this is cheap).
2. Print the summary to stderr along with any active warnings (high orphan rate, skipped symlinked dirs).
3. If the high-orphan-rate check fired and `--allow-high-orphan-rate` was not passed, refuse with code 3.
4. If stdin is not a TTY and `--yes` was not passed, refuse with code 3 and a message instructing the user to either run interactively or pass `--yes`.
5. Otherwise prompt `Proceed? [y/N]`. Anything other than `y` / `Y` / `yes` aborts (exit 0, nothing deleted).
6. With `--yes`, skip the prompt entirely.
7. Pass the orphan list to the deleter, optionally with a `trash_dir`.

### `delete.py` — deletion with optional trash destination

`delete_orphans` gains an optional `trash_dir: Path | None = None` parameter.

- When `trash_dir is None`: today's behaviour. `path.unlink()` per orphan.
- When `trash_dir is not None`: for each orphan, compute `target = trash_dir / results_dir_name / rel`, ensure parents exist with `os.makedirs(parents=True, exist_ok=True)`, then `shutil.move(orphan.path, target)`. The `results_dir_name` segment is included so a single trash dir can be safely reused across multiple results dirs without collision.

`OrphanFile` gains a `rel: str` field — the rel-posix path under the results dir, which the walker already computes. This avoids recomputing it in the deleter and keeps the relative-path semantics in one place (the walker).

Symlink refusal (`allow_symlinks` flag) is preserved unchanged for both unlink and trash modes — a symlink in the orphan list is still refused unless explicitly allowed.

### `walker.py` — surface skipped symlinked directories

`iter_results_files` gains an optional `stats: dict | None = None` keyword. When passed:

- The walker initialises `stats["skipped_symlinked_dirs"] = 0` on entry, then increments it once for each directory entry that is a symlink to a directory and is being skipped (`follow_symlinks=False`).

The CLI passes a fresh `stats = {}` dict, and after the walk prints a one-line summary on stderr if the count is > 0:

> Skipped N symlinked subdirectory(ies); files reachable only via those paths were not scanned.

`stats` defaults to `None` so existing callers and tests are unaffected.

### CLI surface

New flags on `snakeprune scan`:

```
--allow-empty-rules            Bypass refusal when the workflow has 0 output patterns
--allow-basename-mismatch      Bypass refusal when no rule writes under <results_dir>/
--allow-high-orphan-rate       Bypass --delete refusal when orphan rate exceeds threshold
--orphan-rate-threshold FLOAT  Threshold (0.0–1.0) above which the high-rate warning fires (default 0.5; pass 1.0 to disable)
--yes                          Skip the interactive Y/N prompt before deleting
--trash DIR                    Move orphans to DIR instead of unlinking; implies delete mode
```

No existing flag changes meaning. `--delete` alone still unlinks.

## Behaviour specification

- **Empty rule list** → exit 3 unless `--allow-empty-rules`.
- **No rule prefix matches results-dir basename** → exit 3 unless `--allow-basename-mismatch`.
- **Orphan rate > threshold** → loud warning printed always; with `--delete` / `--trash`, exit 3 unless `--allow-high-orphan-rate`.
- **`--delete` without `--yes` and stdin not a TTY** → exit 3 with guidance.
- **`--delete` with TTY and no `--yes`** → prompt; abort cleanly on anything other than `y` / `yes`.
- **`--trash DIR` without `--delete`** → equivalent to `--delete --trash DIR`.
- **`--trash DIR` with `--delete`** → trash mode wins (move, not unlink).
- **Trash destination layout** → `DIR / <results_dir_name> / <rel>` for each orphan; missing intermediate dirs created.
- **Skipped symlinked dirs** → counted and surfaced as a one-line summary if any were skipped.

**Backwards-compatibility note.** This is a deliberate UX change for `--delete`: today a script running `snakeprune scan ... --delete` silently unlinks; under this design the same invocation refuses (non-TTY + no `--yes`). Existing scripts will need to add `--yes` after a one-time review. All non-`--delete` flows are unchanged for well-formed workflows.

## Testing

New tests in the existing files (no new modules):

- `test_cli.py`:
  - empty rule list → exit code 3, helpful message
  - `--allow-empty-rules` bypasses the empty-rule refusal
  - basename mismatch → exit code 3, lists actual prefixes
  - `--allow-basename-mismatch` bypasses
  - high orphan rate → warning printed; `--delete` refused without `--allow-high-orphan-rate`
  - `--orphan-rate-threshold 1.0` disables the check
  - `--delete` with TTY and `--yes` skips prompt
  - `--delete` with no TTY and no `--yes` → exit 3
  - skipped-symlink-dir summary appears when relevant
  - `--trash DIR` without `--delete` triggers move
  - `--trash DIR` creates target dir if missing
  - `--trash DIR` mirrors `<results_dir_name>/<rel>` structure
- `test_walker.py`:
  - `stats={}` populates `skipped_symlinked_dirs` when symlinked dirs exist
  - `stats={}` stays at zero when no dir symlinks exist
  - default behaviour (no `stats` kwarg) is unchanged
- `test_delete.py`:
  - `delete_orphans(..., trash_dir=...)` moves files to expected locations
  - trash mode preserves rel structure
  - trash mode refuses symlinks unless `allow_symlinks=True` (same as unlink mode)

Interactive prompt is tested using Typer's `CliRunner(input="y\n")` / `CliRunner(input="n\n")`. Non-TTY behaviour is tested by simulating non-TTY stdin (Typer's runner is non-TTY by default, so the `--yes` requirement under non-TTY is naturally exercised).

## README updates

A new top-level section, **`Safety and limitations`**, placed between `Usage` and `Runtime requirements`. Covers:

- The new safety checks (empty rules, basename mismatch, high orphan rate) and their bypass flags.
- The deletion flow: dry-run by default, interactive confirmation under `--delete`, `--yes` for scripts, `--trash DIR` for reversibility.
- Limitations the tool can't detect: workflows whose rule set varies with config; conditionally-imported rules; symlinked subdirectories (with the new tally as a partial mitigation); module imports work but unusual configurations (heavy `with: output:` overrides, nested modules) are less tested.
- Recommended workflow for first use: always run a plain `scan` first, eyeball the orphan list, then add `--delete` (or `--trash`).

The `Usage` section gains a `--trash` example and a `--delete --yes` example.

## Out of scope

- Background or async deletion (orphan counts in real pipelines are modest enough that synchronous unlink is fine).
- A "undo last delete" subcommand (covered by `--trash` for users who want reversibility).
- Workflow re-validation across config variants (would require either running snakeprune multiple times with different configs, or driving Snakemake's DAG with multiple parameter sets — both bigger projects).
