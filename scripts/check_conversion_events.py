#!/usr/bin/env python3
"""Assertions for the conversion-event catalogue (synthesis/conversion_events.py).

The de-duplication rules are subtle enough that a regression would be invisible:
a wrong canonical name still renders, a missed alias just adds a plausible-looking
extra row to a dropdown, and a family collapsed too eagerly silently halves a
count. Each case below is one shape observed in live Meta responses.

    python3 scripts/check_conversion_events.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from intel.synthesis.conversion_events import (  # noqa: E402
    PURCHASE_SENTINEL, build_catalog, default_event, per_ad_counts,
)

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def rows(per_ad: dict[str, dict[str, float]],
         values: dict[str, dict[str, float]] | None = None) -> list[dict]:
    """`{ad_id: {action_type: count}}` → the ad_performance row shape."""
    values = values or {}
    return [
        {"platform_ad_id": ad,
         "extra_json": json.dumps({
             "actions": [{"action_type": t, "value": str(v)}
                         for t, v in acts.items()],
             "action_values": [{"action_type": t, "value": str(v)}
                               for t, v in (values.get(ad) or {}).items()],
         })}
        for ad, acts in per_ad.items()
    ]


print("attribution surfaces collapse to one named event")
# `lead` and its pixel surface differ by two — the live Spectrum shape. Value
# identity would split them; the named family must not.
cat = build_catalog(rows({
    "a1": {"lead": 300, "onsite_web_lead": 300, "offsite_conversion.fb_pixel_lead": 299},
    "a2": {"lead": 196, "onsite_web_lead": 196, "offsite_conversion.fb_pixel_lead": 195},
}))
check("one event", [e.key for e in cat], ["lead"])
check("first-match count, not a sum", cat[0].total, 496.0)
check("singular label", cat[0].one, "lead")

print("\nconversion sets are dropped, base event keeps the name")
# Spectrum's seven identical 737s. Only the call connect should survive, and it
# must not be named after a purchase on an account that has none.
cat = build_catalog(rows({
    "a1": {"click_to_call_native_20s_call_connect": 737,
           "offsite_purchase_add_20_s_calls": 737,
           "offsite_add_to_cart_add_20_s_calls": 737,
           "grouped_pixel_custom_conversions_add_20_s_calls": 737,
           "click_to_call_native_call_placed": 2997},
    "a2": {"click_to_call_native_20s_call_connect": 1,
           "offsite_purchase_add_20_s_calls": 1,
           "click_to_call_native_call_placed": 3},
}))
check("set aliases gone", sorted(e.key for e in cat),
      ["call_connect_20s", "call_placed"])
check("no purchase-named call", any("purchase" in e.label.lower() for e in cat), False)

print("\nvalue-identical unknowns collapse, better name wins")
cat = build_catalog(rows({
    "a1": {"call_confirm_grouped": 10, "click_to_call_call_confirm": 10},
    "a2": {"call_confirm_grouped": 5, "click_to_call_call_confirm": 5},
}))
check("one event", len(cat), 1)
check("ungrouped name wins", cat[0].key, "call_confirmed")

print("\nengagement is never offered")
cat = build_catalog(rows({
    "a1": {"page_engagement": 5000, "post_engagement": 5000, "video_view": 900,
           "link_click": 800, "post_reaction": 90, "lead": 4},
    "a2": {"page_engagement": 4000, "post_reaction": 50, "lead": 6},
}))
check("only the lead survives", [e.key for e in cat], ["lead"])

print("\ndefault selection")
lead_rows = rows({"a1": {"lead": 10, "click_to_call_native_call_placed": 400},
                  "a2": {"lead": 12, "click_to_call_native_call_placed": 380},
                  "a3": {"lead": 8}})
cat = build_catalog(lead_rows)
# Revenue outranks everything, and it must resolve to the sentinel so a
# commerce dashboard reads the columns it always read.
check("revenue wins", default_event(cat, has_revenue=True), PURCHASE_SENTINEL)
# Without revenue, kind priority decides — lead over call, on fewer conversions
# but wider ad coverage.
check("lead over call", default_event(cat, has_revenue=False), "lead")

print("\nper-ad counts roll up to the catalogue total")
counts, _ = per_ad_counts(lead_rows, cat)
lead_ev = next(e for e in cat if e.key == "lead")
check("rollup matches", sum(c.get("lead", 0) for c in counts.values()), lead_ev.total)

print("\nmin-ads floor")
cat = build_catalog(rows({"a1": {"lead": 500}}))
check("single-ad event dropped", cat, [])

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S)")
    sys.exit(1)
print("all conversion-event checks passed")
