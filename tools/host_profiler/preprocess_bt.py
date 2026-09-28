#!/usr/bin/env python3
"""Inject TARGET_PID / TARGET_COMM / MONO_SO into a .bt script.

bpftrace on this host does not support -D, and does not expand #define
inside uprobe:PATH:sym paths. So we textual-substitute placeholders.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _drop_mono_blocks(text: str) -> str:
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if re.match(r"^(u(ret)?probe|usdt):MONO_SO:", lines[i]):
            depth = 0
            entered = False
            while i < len(lines):
                depth += lines[i].count("{") - lines[i].count("}")
                entered = entered or "{" in lines[i]
                i += 1
                if entered and depth <= 0:
                    break
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("script", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--comm", default="7DaysToDieServe")
    ap.add_argument("--mono-so", default="")
    args = ap.parse_args()

    # The output runs as root via sudo bpftrace, so a value that breaks out of
    # its literal would be root code execution. comm/mono-so come from /proc, but
    # a compromised target could craft them - validate rather than trust.
    if any(c in args.comm for c in '"\\\n\r'):
        ap.error("--comm contains characters that would break the bpftrace string literal")
    if args.mono_so.strip():
        mono_path = Path(args.mono_so.strip())
        if not re.fullmatch(r"[\w./+\-]+", str(mono_path)):
            ap.error("--mono-so has characters that could inject bpftrace (expected a plain path)")
        if not mono_path.is_file():
            ap.error(f"--mono-so is not a file: {mono_path}")

    text = args.script.read_text(encoding="utf-8", errors="replace")

    # Drop preprocessor guards from source templates
    text = re.sub(r"#ifndef TARGET_PID\n#error[^\n]*\n#endif\n?", "", text)
    text = re.sub(r"#ifndef MONO_SO\n#error[^\n]*\n#endif\n?", "", text)
    text = re.sub(r"#ifndef TARGET_COMM\n#define TARGET_COMM[^\n]*\n#endif\n?", "", text)
    text = re.sub(r"#define TARGET_COMM[^\n]*\n", "", text)

    # Numeric / string literals that bpftrace macro-expands if #define works;
    # still emit defines for expressions like /pid == TARGET_PID/
    header = f'#define TARGET_PID {args.pid}\n#define TARGET_COMM "{args.comm}"\n'

    mono = args.mono_so.strip()
    if mono:
        # Direct path substitution for uprobe:/path:sym (no spaces if symlink used)
        text = text.replace("uprobe:MONO_SO:", f"uprobe:{mono}:")
        text = text.replace("uretprobe:MONO_SO:", f"uretprobe:{mono}:")
        text = text.replace("usdt:MONO_SO:", f"usdt:{mono}:")
    else:
        # No library: remove each mono probe block entirely (header, optional
        # predicate, and braced body); a dangling predicate is a syntax error.
        text = _drop_mono_blocks(text)

    args.output.write_text(header + text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
