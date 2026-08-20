"""The FACTORY_MEMORY priming receipt: what an agent reports it actually loaded.

`control.runner.parse_memory_receipt` is the pure counterpart to the prompt contract proved
in `control/prompt_profile_test.py`: it turns one machine-readable line back into
`{"indexed", "opened", "domains"}`, or into nothing at all when the line is missing,
malformed, or just ordinary agent chatter that happens to share no shape with a receipt.

Run it directly, no framework needed:

    .venv/bin/python -m control.memory_receipt_test
"""
import json
import sys

from control import runner

fails: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {name}\n       got={got!r}")
    if not ok:
        fails.append(name)


MARKER = runner.MEMORY_RECEIPT_MARKER

# ---------- AC2: a valid receipt yields its indexed count, opened IDs, and domains

valid = f'{MARKER} {{"indexed": 9, "opened": ["mem_acac", "mem_5b24"], "domains": ["repository"]}}'
got = runner.parse_memory_receipt(valid)
check("a bare receipt parses", got is not None, True)
check("the indexed count comes through", got["indexed"], 9)
check("the opened ids come through, in order", got["opened"], ["mem_acac", "mem_5b24"])
check("the domains come through", got["domains"], ["repository"])

surrounded = (
    "some setup chatter\n"
    "$ .venv/bin/python -m control.memory_receipt_test\n"
    f"{valid}\n"
    "more chatter after it\n"
)
check("it is found among other run output", runner.parse_memory_receipt(surrounded), got)

leading_space = f"   {valid}   "
check("surrounding whitespace on the line is tolerated",
      runner.parse_memory_receipt(leading_space), got)

empty = f"{MARKER} {{\"indexed\": 0, \"opened\": [], \"domains\": []}}"
check("the explicit empty receipt parses to the same shape",
      runner.parse_memory_receipt(empty), runner.EMPTY_MEMORY_RECEIPT)
check("build_prompt's own empty-receipt text round-trips",
      runner.parse_memory_receipt(f"{MARKER} " + json.dumps(runner.EMPTY_MEMORY_RECEIPT)),
      runner.EMPTY_MEMORY_RECEIPT)

# ---------- AC3: malformed receipts and ordinary output produce no retrieval event, and
# none of them raise

no_marker = "I opened mem_acac and mem_5b24 from the repository domain."
check("prose that merely mentions ids is not a receipt", runner.parse_memory_receipt(no_marker), None)

bad_json = f"{MARKER} {{not json at all"
check("a marker with unparseable JSON after it yields nothing",
      runner.parse_memory_receipt(bad_json), None)

wrong_shape = f'{MARKER} {{"indexed": 9, "opened": "mem_acac", "domains": ["repository"]}}'
check("opened must be a list, not a bare string", runner.parse_memory_receipt(wrong_shape), None)

missing_field = f'{MARKER} {{"indexed": 9, "opened": ["mem_acac"]}}'
check("a receipt missing a required field is rejected", runner.parse_memory_receipt(missing_field), None)

wrong_types = f'{MARKER} {{"indexed": "nine", "opened": [], "domains": []}}'
check("indexed must be an int, not a string", runner.parse_memory_receipt(wrong_types), None)

bool_indexed = f'{MARKER} {{"indexed": true, "opened": [], "domains": []}}'
check("a bool is not an int here, even though Python thinks so",
      runner.parse_memory_receipt(bool_indexed), None)

negative = f'{MARKER} {{"indexed": -1, "opened": [], "domains": []}}'
check("a negative count is rejected", runner.parse_memory_receipt(negative), None)

non_string_items = f'{MARKER} {{"indexed": 1, "opened": [1, 2], "domains": []}}'
check("opened entries must be strings", runner.parse_memory_receipt(non_string_items), None)

lookalike_prefix = f"{MARKER}ISH not actually the marker {{\"indexed\": 1, \"opened\": [], \"domains\": []}}"
check("a line that only starts with the marker's letters is not the marker",
      runner.parse_memory_receipt(lookalike_prefix), None)

check("no receipt anywhere in ordinary output", runner.parse_memory_receipt("just a normal log\nnothing here"), None)
check("empty string does not raise", runner.parse_memory_receipt(""), None)
check("None-shaped input does not raise", runner.parse_memory_receipt(None), None)

print()
print(f"{len(fails)} failed" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
