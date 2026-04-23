"""Single home for the price-scrub helper used across narrative generators.

The rewriter + scout LLMs occasionally embed raw odds in card copy
("stacks at 4.41", "Draw No Bet at 1.44 is where this lands"). Those
numbers are captured at card-generation time but the displayed leg odds
re-quote via SSE and drift — the user sees "pays 4.41" in the hook while
the UI leg prices combine to 4.62. Credibility gap.

The fix is twofold:
  1. Prompt-level instruction ("Do NOT include any numeric odds...")
  2. Defensive post-process scrub via `strip_prices(text)` for anything
     that slips through.

Keep this module narrow: just regex patterns + one function. No LLM,
no state, no dependencies beyond `re`. Unit test at bottom.
"""
from __future__ import annotations

import re

# Odds patterns — decimal form is the only shape we emit anywhere in the
# product today. Deliberately conservative: match a decimal number with
# 1-2 fractional digits, and only strip it when it's adjacent to a price
# verb ("at", "pays", "odds of", etc.) OR preceded by a bare "@".
#
# Why we don't just strip all "N.NN" tokens: player-stat copy legitimately
# uses decimals ("3.5 goals a game", "1.2 xG"). We want to keep those.

# Lone `@ 4.41` or `@4.41`
_AT_SYMBOL = re.compile(r"\s*@\s*\d+\.\d{1,2}", flags=re.IGNORECASE)

# "(price-verb) 4.41" — covers pays / at / odds of / stacks at / stacked at /
# paying / priced at / lands at / quoted at.
#
# Two-group structure: group 1 is a verb we KEEP (stacks, stacked), group 2
# is connective we DROP (at, pays, odds of). This lets "stacks at 4.41"
# collapse to "stacks" while "at 1.68" collapses to "".
_PRICE_VERB_KEEP = re.compile(
    r"\b(stacks|stacked)\s+(?:at|for)\s+\d+\.\d{1,2}\b",
    flags=re.IGNORECASE,
)
_PRICE_VERB_DROP = re.compile(
    r"\b(pays|paying|at|odds of|priced at|lands at|quoted at)\s+\d+\.\d{1,2}\b",
    flags=re.IGNORECASE,
)

# "in total pays 4.41" / "in total paying 4.41"
_TOTAL_PAY = re.compile(
    r"\bin total pay(s|ing)?\s+\d+\.\d{1,2}\b",
    flags=re.IGNORECASE,
)

# Trailing `— 4.41.` or ` - 4.41.` or ` — 4.41` at end-of-sentence. These
# are dangling price tags the rewriter sometimes appends to an otherwise
# clean angle. Only match when the decimal is bordered by a dash/em-dash
# so we don't eat legitimate numerics. The optional trailing punctuation
# is also consumed so the caller isn't left with a lone "." at EOS.
_TRAILING_DASH = re.compile(
    r"\s*[\-\u2014\u2013]\s*\d+\.\d{1,2}[.!?;,]?\s*$"
)

# After stripping, we may be left with awkward artifacts:
#   - double spaces
#   - " , " or " ." (orphan punctuation)
#   - trailing " —" or " -"
_DOUBLE_SPACE = re.compile(r"\s{2,}")
_ORPHAN_PUNCT = re.compile(r"\s+([,.;:!?])")
_TRAILING_DASH_ORPHAN = re.compile(r"\s*[\-\u2014\u2013]\s*([.!?;,]|$)")


def strip_prices(text: str) -> str:
    """Scrub price-like constructs from a narrative string.

    Conservative: targets known price-verb patterns and bare `@ N.NN`.
    Leaves unrelated decimals intact ("3.5 goals", "1.2 xG", "2-1 loss",
    "one goal in four", "3+ goals").

    Returns cleaned text with whitespace + orphan punctuation tidied up.
    """
    if not text:
        return text or ""

    out = text
    out = _AT_SYMBOL.sub("", out)
    out = _TOTAL_PAY.sub("", out)   # run before price-verb matchers so "pays" doesn't double-match
    out = _PRICE_VERB_KEEP.sub(lambda m: m.group(1), out)  # keep "stacks"/"stacked", drop rest
    out = _PRICE_VERB_DROP.sub("", out)  # drop the whole construct
    out = _TRAILING_DASH.sub("", out)

    # Cleanup pass
    out = _TRAILING_DASH_ORPHAN.sub(r"\1", out)
    out = _ORPHAN_PUNCT.sub(r"\1", out)
    out = _DOUBLE_SPACE.sub(" ", out).strip()

    # A price connective followed by nothing meaningful often leaves a
    # dangling "at" / "pays" / "odds of" at the end of a phrase after the
    # number was scrubbed. Strip those — but ONLY if they're at end of
    # string / before punctuation. Never strip these words mid-sentence
    # (e.g. "at home" must survive).
    out = re.sub(
        r"\s+(pays|paying|at|odds of|priced at|lands at|quoted at|in total pays|in total paying)(?=[.!?;,]|$)",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = _ORPHAN_PUNCT.sub(r"\1", out)
    out = _DOUBLE_SPACE.sub(" ", out).strip()
    # Final trim: strip leading/trailing punctuation artifacts like " ,"
    out = out.strip(" ,;:-\u2014\u2013")
    return out


if __name__ == "__main__":
    cases: list[tuple[str, str]] = [
        # Spec acceptance cases
        ("stacks at 4.41", "stacks"),
        ("Over 2.5 at 1.68 is where this lands", "Over 2.5 is where this lands"),
        ("2-1 loss", "2-1 loss"),
        ("one goal in four", "one goal in four"),
        ("3+ goals", "3+ goals"),

        # Realistic card headlines / angles from the spike output
        ("all of it pays 4.41", "all of it"),
        ("stacked at 2.83", "stacked"),
        ("all three find the net at 6.16", "all three find the net"),
        ("De Jong back from suspension; Barcelona to win + Over 2.5 + the man himself to find the net — 4.62.",
         "De Jong back from suspension; Barcelona to win + Over 2.5 + the man himself to find the net"),
        ("Draw No Bet on Rayo at 1.44 is where this story lands",
         "Draw No Bet on Rayo is where this story lands"),
        ("Palmer to score @ 2.40, Chelsea to win", "Palmer to score, Chelsea to win"),
        ("in total pays 18.40", ""),
        ("in total paying 6.75", ""),
        ("odds of 5.50 on the goalscorer", "on the goalscorer"),
        ("priced at 1.98 is the value play", "is the value play"),

        # Player-stat style that MUST be preserved
        ("3.5 goals per game", "3.5 goals per game"),
        ("1.2 xG against", "1.2 xG against"),
        ("scored 14 goals this season", "scored 14 goals this season"),
        ("four wins in five", "four wins in five"),

        # Empty / edge
        ("", ""),
    ]

    failed = 0
    for raw, expected in cases:
        got = strip_prices(raw)
        if got != expected:
            failed += 1
            print(f"FAIL: strip_prices({raw!r})")
            print(f"  expected: {expected!r}")
            print(f"  got:      {got!r}")
        else:
            print(f"ok: {raw!r} -> {got!r}")

    if failed:
        print(f"\n{failed} FAILED")
        raise SystemExit(1)
    print("\nPASS")
