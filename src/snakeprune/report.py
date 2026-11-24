"""Orphan bookkeeping that stays small however large the results tree is.

``OrphanSpool`` keeps the orphan list in an anonymous temporary file rather
than in memory, so the walk's memory use does not grow with the number of
orphans. ``DirectorySummary`` aggregates orphans by leading directory for
``--summary``. ``AmbiguityExplainer`` names the rule and the unconstrained
wildcards behind each ambiguous orphan.
"""
from __future__ import annotations

import os
import re
import struct
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from snakeprune.patterns import (
    DEFAULT_WILDCARD_REGEX,
    SNAKEMAKE_WILDCARD_REGEX,
    extract_literal_prefix,
)
from snakeprune.walker import OrphanFile

# Record: flags (B), path length (I), offset of rel within path (I),
# likely-rule length (H), then the path bytes and the likely-rule bytes.
_HEADER = struct.Struct("<BIIH")
_AMBIGUOUS = 1


class OrphanSpool:
    """Append-only orphan list backed by an anonymous temporary file.

    Paths are stored as their ``os.fsencode`` bytes, so names containing
    newlines or undecodable bytes round-trip exactly. The file lives in the
    system temporary directory (``TMPDIR`` is honoured) and disappears when
    the spool is closed or the process exits.

    Iterating yields ``OrphanFile`` objects; the spool can be iterated any
    number of times, which lets deletion validate every entry before removing
    the first.
    """

    def __init__(self) -> None:
        self._file = tempfile.TemporaryFile(prefix="snakeprune-")  # noqa: SIM115 (closed by close())
        self.count = 0
        self.ambiguous_count = 0

    def add(self, full_path: str, rel: str, ambiguous: bool, likely_rule: str | None) -> None:
        path_bytes = os.fsencode(full_path)
        rel_offset = len(path_bytes) - len(os.fsencode(rel))
        likely_bytes = (likely_rule or "").encode("utf-8")
        flags = _AMBIGUOUS if ambiguous else 0
        self._file.write(
            _HEADER.pack(flags, len(path_bytes), rel_offset, len(likely_bytes))
            + path_bytes
            + likely_bytes
        )
        self.count += 1
        self.ambiguous_count += ambiguous

    def finish(self) -> None:
        """Flush buffered records to disk; raises ``OSError`` if any write failed."""
        self._file.flush()
        os.fsync(self._file.fileno())

    def iter_orphans(self, include_ambiguous: bool = True) -> Iterator[OrphanFile]:
        self._file.seek(0)
        read = self._file.read
        swap_sep = os.sep != "/"
        while True:
            header = read(_HEADER.size)
            if not header:
                return
            if len(header) != _HEADER.size:
                raise OSError("orphan spool is truncated")
            flags, path_len, rel_offset, likely_len = _HEADER.unpack(header)
            body = read(path_len + likely_len)
            if len(body) != path_len + likely_len:
                raise OSError("orphan spool is truncated")
            ambiguous = bool(flags & _AMBIGUOUS)
            if ambiguous and not include_ambiguous:
                continue
            path_bytes = body[:path_len]
            rel = os.fsdecode(path_bytes[rel_offset:])
            if swap_sep:
                rel = rel.replace(os.sep, "/")
            likely = body[path_len:].decode("utf-8") or None
            yield OrphanFile(
                path=Path(os.fsdecode(path_bytes)),
                rel=rel,
                likely_rule=likely,
                ambiguous=ambiguous,
            )

    def close(self) -> None:
        self._file.close()


class OrphanSelection:
    """A re-iterable view of a spool, e.g. the orphans that will be deleted."""

    def __init__(self, spool: OrphanSpool, include_ambiguous: bool) -> None:
        self._spool = spool
        self._include_ambiguous = include_ambiguous

    def __iter__(self) -> Iterator[OrphanFile]:
        return self._spool.iter_orphans(include_ambiguous=self._include_ambiguous)

    def __len__(self) -> int:
        if self._include_ambiguous:
            return self._spool.count
        return self._spool.count - self._spool.ambiguous_count


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024
        if size < 1024 or unit == "PB":
            return f"{size:.1f} {unit}"
    raise AssertionError("unreachable")


@dataclass
class _Group:
    deletable: int = 0
    ambiguous: int = 0
    size: int = 0


class DirectorySummary:
    """Orphan counts and sizes grouped by the first ``depth`` directories."""

    def __init__(self, depth: int) -> None:
        if depth < 1:
            raise ValueError("summary depth must be at least 1")
        self.depth = depth
        self._groups: dict[str, _Group] = {}

    def add(self, rel: str, deletable: bool, size: int) -> None:
        parts = rel.split("/")[:-1][: self.depth]
        key = "/".join(parts) + "/" if parts else "./"
        group = self._groups.get(key)
        if group is None:
            group = self._groups[key] = _Group()
        if deletable:
            group.deletable += 1
        else:
            group.ambiguous += 1
        group.size += size

    def render(self) -> list[str]:
        rows = sorted(
            self._groups.items(),
            key=lambda kv: (-(kv[1].deletable + kv[1].ambiguous), kv[0]),
        )
        total = _Group()
        lines = [f"{'deletable':>12}  {'ambiguous':>12}  {'size':>10}  directory"]
        for key, g in rows:
            total.deletable += g.deletable
            total.ambiguous += g.ambiguous
            total.size += g.size
            lines.append(
                f"{g.deletable:>12}  {g.ambiguous:>12}  {_human_size(g.size):>10}  {key}"
            )
        lines.append(
            f"{total.deletable:>12}  {total.ambiguous:>12}  "
            f"{_human_size(total.size):>10}  (total)"
        )
        return lines


@dataclass
class _Explanation:
    count: int = 0
    example_rel: str = ""
    example_values: dict[str, str] = field(default_factory=dict)


class AmbiguityExplainer:
    """Attribute ambiguous orphans to the rule pattern that claims them.

    ``patterns`` are the permissive (``.+``) rule patterns and ``strict``
    the same rules built with the default ``[^/]+``, in the same order. For
    each ambiguous orphan the explainer finds the first pattern that matches
    and records every wildcard in it that has no constraint, because any of
    them may be the one absorbing the extra directory level: in ``{a}/{b}``
    with both unconstrained, constraining only ``a`` leaves ``b`` free to
    span ``/``. A wildcard is unconstrained when its group body is the
    default in both builds (alone, or leading a union of output
    constraints), so one explicitly constrained to ``.+`` is not reported.
    """

    MAX_SHOWN = 20

    def __init__(
        self,
        patterns: list[tuple[str, re.Pattern]],
        strict: list[tuple[str, re.Pattern]] | None = None,
        fallback=None,
    ) -> None:
        # ``fallback`` (an ``InputMatcher`` built with ``.+``) attributes files
        # that only an input pattern claims. Input wildcards carry no explicit
        # constraint of their own, so a ``.+`` body there means unconstrained.
        self._fallback = fallback
        self._fallback_free: dict[int, tuple[str, ...]] = {}
        aligned = strict is not None and len(strict) == len(patterns) and all(
            a[0] == b[0] for a, b in zip(strict, patterns)
        )
        self._patterns = []
        for i, (name, rx) in enumerate(patterns):
            free = self._groups_with_body(rx, SNAKEMAKE_WILDCARD_REGEX)
            if aligned:
                assert strict is not None
                default = set(self._groups_with_body(strict[i][1], DEFAULT_WILDCARD_REGEX))
                free = tuple(w for w in free if w in default)
            self._patterns.append((name, rx, extract_literal_prefix(rx), free))
        self._found: dict[tuple[str, tuple[str, ...]], _Explanation] = {}

    @staticmethod
    def _groups_with_body(rx: re.Pattern, body: str) -> tuple[str, ...]:
        # The body alone, or first in a union of a rule's output constraints
        # (`[^/]+|(?:\d+)`, see `rule_wildcard_bodies`), which is
        # unconstrained because some output leaves the wildcard free.
        escaped = re.escape(body)
        return tuple(
            m.group(1)
            for m in re.finditer(
                r"\(\?P<(\w+)>" + escaped + r"(?:\)|\|\(\?:)", rx.pattern
            )
        )

    def _find(self, targets: list[str]) -> tuple[int, re.Match[str]] | None:
        # First matching rule in workflow order, so attribution does not
        # depend on the order files are walked.
        for t in targets:
            for i, (_, rx, prefix, _) in enumerate(self._patterns):
                if t.startswith(prefix):
                    m = rx.match(t)
                    if m:
                        return i, m
        return None

    def add(self, rel: str, targets: list[str]) -> None:
        hit = self._find(targets)
        if hit is not None:
            index, match = hit
            name, _, _, unconstrained = self._patterns[index]
        else:
            found = None
            if self._fallback is not None:
                for t in targets:
                    found = self._fallback.find(t)
                    if found:
                        break
            if found is None:
                return
            name, rx, match = found
            unconstrained = self._fallback_free.get(id(rx))
            if unconstrained is None:
                unconstrained = self._groups_with_body(rx, SNAKEMAKE_WILDCARD_REGEX)
                self._fallback_free[id(rx)] = unconstrained
            name = f"{name} (input)"
        key = (name, unconstrained)
        entry = self._found.get(key)
        if entry is None:
            entry = self._found[key] = _Explanation(
                example_rel=rel,
                example_values={w: match.group(w) for w in unconstrained},
            )
        entry.count += 1

    def render(self) -> list[str]:
        if not self._found:
            return []
        rows = sorted(self._found.items(), key=lambda kv: (-kv[1].count, kv[0]))
        lines = ["Ambiguous orphans by rule, with that rule's unconstrained wildcards:"]
        for (rule, wildcards), e in rows[: self.MAX_SHOWN]:
            names = " ".join(f"{{{w}}}" for w in wildcards)
            values = ", ".join(f"{w}={v!r}" for w, v in e.example_values.items())
            lines.append(f"  {rule}: {e.count} file(s); unconstrained: {names}")
            lines.append(f"    e.g. {e.example_rel}")
            lines.append(f"    matched with {values}")
        if len(rows) > self.MAX_SHOWN:
            rest = sum(e.count for _, e in rows[self.MAX_SHOWN:])
            lines.append(
                f"  ... and {len(rows) - self.MAX_SHOWN} more rule(s) covering "
                f"{rest} file(s)."
            )
        lines.append(
            "If these files are stale, constrain every wildcard listed for the "
            "rule (e.g. `wildcard_constraints: name=\"[^/]+\"`, or a tighter "
            "regex); constraining only some can leave another free to span "
            "`/`. They then become ordinary orphans."
        )
        return lines
