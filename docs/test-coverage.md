# snakeprune test coverage: what we check and why you can trust it

This document is written to be read, not skimmed. Its purpose is to let you (or
a colleague who is rightly suspicious of a tool that deletes files) understand
exactly what the test suite guarantees and where its edges are. It is honest
about what is *not* yet covered, because a coverage report that claims
perfection is less trustworthy than one that names its gaps.

As of this writing the suite is **96 tests**, split across four files:

- `tests/test_patterns.py` (35) — does snakeprune correctly understand what a
  Snakemake workflow actually produces?
- `tests/test_walker.py` (16) — does it find the right files on disk?
- `tests/test_delete.py` (10) — does the deletion primitive refuse the things
  it should and only remove the things it should?
- `tests/test_cli.py` (35) — does the whole command-line tool, end to end,
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
and checks that the output patterns it recovers match what Snakemake itself
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
a Snakemake output pattern to an anchored regex escapes literal regex
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
non-existent paths (a no-op, not an error), and the reported tally.

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

4. **The basename-mismatch guard.** If no rule writes under the basename of the
   directory you targeted, that usually means you pointed at the wrong place.
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

9. **`--trash` as a reversible alternative.** Instead of unlinking, you can move
   orphans into a trash directory; the original relative structure is preserved
   under `<trash>/<results-dir-name>/…`, and the namespacing lets one trash
   directory serve multiple results directories without collisions.

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

## What is NOT yet covered (read this part too)

Trust comes from knowing the edges, so here are the gaps that remain:

- **`--limit` combined with `--delete`.** `--limit` exists for benchmarking and
  stops the scan after N files. Nothing currently prevents combining it with
  `--delete`, which would delete orphans from a *partial* scan and compute the
  orphan-rate guard against the truncated count. This is a known footgun; the
  safest fix is probably to make `--limit` refuse to combine with deletion. Not
  yet done.

- **`--delete` with zero orphans.** The no-op path (nothing to delete, no
  prompt, clean exit) is believed correct but is not pinned by an explicit test.

- **Idempotent re-runs.** Running `--delete` twice in a row should find zero
  orphans the second time; not explicitly tested.

- **Trash collisions.** If a previous run already moved a file of the same
  relative path into the trash directory, the overwrite-vs-refuse behaviour is
  undefined and untested.

- **Case-insensitive filesystems** (e.g. default macOS) and **dotfiles**: the
  matcher is case-sensitive and the walker includes dotfiles; neither is
  exercised by a dedicated test, so behaviour there is by inspection rather than
  by proof.

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
