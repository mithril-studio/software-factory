"""Reading `key=value` output back from a machine.

Both things that ask a golden questions — the preflight check and the freshness sweep — do it
the same way: one `exec` running a shell script that prints one `key=value` per line, rather
than a call per question. This is the other half of that, kept in one place so the two agree
about what a missing key means.
"""

from __future__ import annotations


def parse(stdout: str) -> dict[str, str]:
    """Split probe output into a dict. Lines without `=` are ignored.

    A key the script never printed is *absent*, not empty: the command that would have
    printed it failed, and the caller reports that rather than treating it as a value.
    """
    out = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out
