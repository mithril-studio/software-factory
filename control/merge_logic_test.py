"""The two deterministic pieces of merging: deciding a transient GitHub failure from a real
refusal, and pulling the cause out of a failed CI job's log.

Both exist because of runs that are in the record. Issue #51's pull request passed review and
all three checks, then hit a single 503 on the merge call and was stranded for a human with
the reason discarded. Issue #50's `gates` job failed on a Docker Hub timeout, and the factory
— seeing only the name of the check — spent two fix runs trying to repair an outage.

Run it directly, no framework needed:

    .venv/bin/python -m control.merge_logic_test
"""
import asyncio
import sys

import httpx

from control.github import extract_failure, merge_pr

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


# ---------- pulling the cause out of a job log
#
# Shaped like the real thing: an Actions log is timestamped on every line and ends with
# credential teardown, so its *tail* is reliably the least informative part of it.
JOB_LOG = "\n".join(
    [
        "2026-08-17T16:14:34.3566779Z ##[group]Run docker compose up -d db_test",
        "2026-08-17T16:14:34.7856798Z  db_test Pulling ",
        '2026-08-17T16:14:49.9425462Z  db_test Error Head "https://registry-1.docker.io/v2/'
        'library/postgres/manifests/16": net/http: request canceled (Client.Timeout exceeded)',
        "2026-08-17T16:14:49.9489328Z ##[error]Process completed with exit code 1.",
        "2026-08-17T16:14:50.2158391Z Post job cleanup.",
        "2026-08-17T16:14:50.2832653Z Cleaning up orphan processes",
    ]
)

found = extract_failure(JOB_LOG)
check("keeps the line that names the cause", "registry-1.docker.io" in found, True)
check("keeps the error marker", "##[error]" in found, True)
check("drops the timestamp prefix", "2026-08-17T16:14" in found, False)

# The bug this pins down: the tail of a real Actions log is credential teardown, so a
# tail-based excerpt of a failed job reliably omits the only part worth reading. However far
# the log runs on past the marker, the excerpt must still lead with the cause.
padded = JOB_LOG + "\n" + "\n".join(
    f"2026-08-17T16:15:0{i % 10}.0Z teardown step {i}" for i in range(60)
)
padded += "\n2026-08-17T16:16:00.0Z LAST_LINE_OF_THE_JOB"
found_padded = extract_failure(padded)
check(
    "still leads with the cause when the log runs on",
    "registry-1.docker.io" in found_padded,
    True,
)
check("drops teardown beyond the window", "LAST_LINE_OF_THE_JOB" in found_padded, False)

check(
    "no marker -> falls back to the tail",
    extract_failure("aa\nbb\ncc\ndd", max_chars=5),
    "cc\ndd",
)
check("empty log -> empty", extract_failure(""), "")
check(
    "respects the character budget",
    len(extract_failure("x" * 200 + "\n##[error]boom", max_chars=40)) <= 40,
    True,
)


# ---------- retrying GitHub, but only when GitHub is the problem


class FakeResponse:
    """A merge response. Error bodies look like GitHub's, because the message is the point.

    A refusal used to be raised as `raise_for_status()` wrote it — the status and nothing
    else. That is how a token which had lost its write grants presented as a bare
    "403 Forbidden", indistinguishable from rate limiting or a branch rule, and it took a
    live probe against the API to tell which. The body and the
    `x-accepted-github-permissions` header both say it outright.
    """

    def __init__(self, code, body=None, headers=None):
        self.status_code = code
        self.request = httpx.Request("PUT", "http://example.invalid")
        self._body = body if body is not None else {"merged": True}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


sent: list[dict] = []


def run_merge(statuses, **kwargs):
    """Drive merge_pr through a scripted sequence of responses.

    Every payload it sent lands in `sent`, which is readable after a raise as well as after a
    return — the no-retry cases are precisely the ones that raise, and "how many times did it
    call GitHub" is the whole assertion.
    """
    sent.clear()

    def put(self, url, **kw):
        sent.append(kw.get("json"))

        async def respond():
            spec = statuses[min(len(sent) - 1, len(statuses) - 1)]
            # A bare int stays a bare int; a tuple carries the body and headers a real
            # refusal would.
            return FakeResponse(*spec) if isinstance(spec, tuple) else FakeResponse(spec)

        return respond()

    original, httpx.AsyncClient.put = httpx.AsyncClient.put, put
    try:
        # backoff=0 so the retry policy is tested without waiting out its delays.
        return asyncio.run(merge_pr("o/r", 1, sha="abc", backoff=0, **kwargs))
    finally:
        httpx.AsyncClient.put = original


check("503 twice then 200 -> merged", run_merge([503, 503, 200]), {"merged": True})
check("...after exactly three calls", len(sent), 3)
check("every attempt pins the verified sha", [p.get("sha") for p in sent], ["abc"] * 3)
check("squash stays the default", sent[0].get("merge_method"), "squash")

for code in (500, 502, 504, 429):
    run_merge([code, 200])
    check(f"{code} is retried", len(sent), 2)

# A refusal is an answer, and answers must not be retried: 405 means the pull request is not
# mergeable, 409 means the head moved. Retrying either would spin pointlessly, or — worse —
# eventually land a commit the checks were never run against.
for code in (405, 409, 422, 404):
    try:
        run_merge([code, 200])
        check(f"{code} raises rather than merging", "merged", "raised")
    except httpx.HTTPStatusError:
        check(f"{code} raises at once, no retry", len(sent), 1)

# ---------- a refusal carries GitHub's own explanation, not just its status

REFUSAL = (
    403,
    {"message": "Resource not accessible by personal access token"},
    {"x-accepted-github-permissions": "contents=write"},
)
try:
    run_merge([REFUSAL])
    check("a refusal raises", "merged", "raised")
except httpx.HTTPStatusError as exc:
    check("the refusal names the status", "403" in str(exc), True)
    check("...and quotes GitHub's message rather than dropping it",
          "Resource not accessible by personal access token" in str(exc), True)
    check("...and names the permission that was missing",
          "contents=write" in str(exc), True)

# An error whose body is not a GitHub error object still says something: the raw text, not
# an empty string. 405 rather than 5xx, so this tests the refusal path and not the retry one.
try:
    run_merge([(405, "upstream said no", {})])
    check("a bodyless refusal raises", "merged", "raised")
except httpx.HTTPStatusError as exc:
    check("a non-JSON body is quoted rather than swallowed", "upstream said no" in str(exc), True)

try:
    run_merge([503], attempts=3)
    check("exhausted retries raise", "merged", "raised")
except httpx.HTTPStatusError as exc:
    check("exhausted retries raise the last status", exc.response.status_code, 503)
    check("...having used every attempt", len(sent), 3)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
