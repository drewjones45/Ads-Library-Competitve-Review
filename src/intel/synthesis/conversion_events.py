"""Which conversions an owned ad account actually produces, discovered from delivery.

The dashboard was built for a retailer, so its outcome column is ROAS and its
conversion count is `purchases`. On a lead-generation account both are
structurally zero — Spectrum Reach reports $0 revenue and 0 purchases across
every ad — so the outcome half of every attribute table reads `0.00` and the
ROAS index is a column of dashes. The conversions are not missing; they are
sitting in `ad_performance.extra_json` under names nothing ever reads:
496 `lead`, 2,997 `click_to_call_native_call_placed`.

This module turns that blob into a short, selectable catalogue.

The hard part is not finding the events, it is that Meta reports the SAME
conversion many times over. Spectrum's 48 raw `action_type`s are 35 distinct
conversions; JD Sports' 101 are 78. The duplicates come from three places:

  * attribution surface — `lead`, `onsite_web_lead`, `omni_*`, `onsite_web_*`,
    `offsite_conversion.fb_pixel_*` are one conversion seen four ways;
  * conversion sets — an advertiser-named grouping re-reports its members under
    an `offsite_<event>_add_<set name>` alias, which is why Spectrum shows six
    different events all worth exactly 737;
  * `*_grouped` roll-ups that equal their ungrouped twin.

Two passes handle it. First the known families collapse by name, because names
carry meaning a count cannot: `lead` (496) and `offsite_conversion.fb_pixel_lead`
(494) are the same event even though the numbers differ by two. Then whatever
survives collapses by VALUE IDENTITY — if two action types have the same count
on every single ad, they are one conversion reported twice, and listing both is
noise. Name-matching alone would miss the 737s (no shared stem); vector-matching
alone would split the leads. Together they get both.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------- taxonomy ---
# Kinds, most-decision-relevant first. This order IS the auto-selection
# priority: an account that reports both purchases and leads is a commerce
# account whose lead events are incidental, so purchase outranks lead.
# `engagement` is last and is never offered as an outcome — a page like is not
# a conversion, and letting one be picked as the CPA denominator would produce
# a confident-looking cost-per-like.
KIND_ORDER = ["purchase", "lead", "call", "signup", "message", "app",
              "funnel", "traffic", "other", "engagement"]

KIND_LABEL = {
    "purchase": "Purchase", "lead": "Lead", "call": "Call",
    "signup": "Registration", "message": "Messaging", "app": "App",
    "funnel": "Funnel step", "traffic": "Traffic", "other": "Other",
    "engagement": "Engagement",
}

# Canonical events worth naming explicitly: the alias family that collapses to
# them, a human label, and the kind. Order matters only for readability.
#
# `aliases` is matched EXACTLY against `action_type`. Anything not listed keeps
# its raw name and is classified by the pattern rules below, so a new Meta event
# type degrades to "shown with its raw name" rather than "silently dropped".
_FAMILIES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    # key, label, singular ("cost per ___"), kind, aliases
    ("purchase", "Purchases", "purchase", "purchase",
     ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase",
      "onsite_web_purchase", "onsite_web_app_purchase", "web_in_store_purchase",
      "web_app_in_store_purchase", "app_custom_event.fb_mobile_purchase",
      "onsite_app_purchase", "onsite_conversion.purchase")),
    ("lead", "Leads", "lead", "lead",
     ("lead", "onsite_web_lead", "offsite_conversion.fb_pixel_lead",
      "onsite_conversion.lead", "onsite_conversion.lead_grouped",
      "omni_lead", "app_custom_event.fb_mobile_lead")),
    ("complete_registration", "Registrations", "registration", "signup",
     ("complete_registration", "omni_complete_registration",
      "offsite_conversion.fb_pixel_complete_registration",
      "onsite_web_complete_registration",
      "app_custom_event.fb_mobile_complete_registration")),
    ("subscribe", "Subscriptions", "subscription", "signup",
     ("subscribe", "offsite_conversion.fb_pixel_subscribe",
      "onsite_conversion.subscribe")),
    ("start_trial", "Trials started", "trial", "signup",
     ("start_trial", "offsite_conversion.fb_pixel_start_trial")),
    ("submit_application", "Applications", "application", "lead",
     ("submit_application", "offsite_conversion.fb_pixel_submit_application")),
    ("schedule", "Appointments booked", "appointment", "lead",
     ("schedule", "offsite_conversion.fb_pixel_schedule")),
    ("contact", "Contacts", "contact", "lead",
     ("contact", "offsite_conversion.fb_pixel_contact")),
    ("find_location", "Store locator uses", "store-locator use", "lead",
     ("find_location", "offsite_conversion.fb_pixel_find_location")),
    ("call_placed", "Calls placed", "call placed", "call",
     ("click_to_call_native_call_placed", "click_to_call_call_now")),
    ("call_confirmed", "Calls confirmed", "confirmed call", "call",
     ("click_to_call_call_confirm", "call_confirm_grouped")),
    ("call_connect_20s", "Calls connected 20s+", "20s+ call connect", "call",
     ("click_to_call_native_20s_call_connect",)),
    ("call_connect_60s", "Calls connected 60s+", "60s+ call connect", "call",
     ("click_to_call_native_60s_call_connect",)),
    ("messaging_started", "Conversations started", "conversation", "message",
     ("onsite_conversion.messaging_conversation_started_7d",
      "onsite_conversion.total_messaging_connection")),
    ("add_to_cart", "Add to cart", "add to cart", "funnel",
     ("omni_add_to_cart", "add_to_cart", "onsite_web_add_to_cart",
      "onsite_web_app_add_to_cart", "offsite_conversion.fb_pixel_add_to_cart")),
    ("initiate_checkout", "Checkouts started", "checkout", "funnel",
     ("omni_initiated_checkout", "initiate_checkout",
      "onsite_web_initiate_checkout",
      "offsite_conversion.fb_pixel_initiate_checkout")),
    ("landing_page_view", "Landing page views", "landing page view", "traffic",
     ("omni_landing_page_view", "landing_page_view")),
    ("view_content", "Content views", "content view", "funnel",
     ("omni_view_content", "view_content", "onsite_web_view_content",
      "onsite_web_app_view_content",
      "offsite_conversion.fb_pixel_view_content")),
    ("search", "Searches", "search", "traffic",
     ("search", "omni_search", "offsite_conversion.fb_pixel_search")),
    ("custom_pixel", "Custom pixel conversions", "custom conversion", "other",
     ("offsite_conversion.fb_pixel_custom",)),
]

_ALIAS_TO_FAMILY: dict[str, tuple[str, str, str]] = {
    alias: (key, label, kind)
    for key, label, _one, kind, aliases in _FAMILIES
    for alias in aliases
}

# Social interaction with the ad or the page. Real, and none of it is an
# outcome you would set a cost target against.
_ENGAGEMENT_EXACT = {
    "page_engagement", "post_engagement", "post", "post_reaction", "comment",
    "like", "photo_view", "rsvp", "event_response", "post_interaction_net",
    "post_interaction_gross", "video_view", "link_click", "checkin",
}
_ENGAGEMENT_PREFIX = ("onsite_conversion.post_", "onsite_conversion.messaging_block")

# A conversion set re-reports its members under `offsite_<event>_add_<set>`.
# These are duplicates by construction — the 737s — and they are also the ones
# whose names actively mislead (`offsite_purchase_add_20_s_calls` counts phone
# calls, not purchases). Never preferred as a group's canonical name.
_CONV_SET_RE = re.compile(r"_add_[a-z0-9_]+$")

_SURFACE_PREFIXES = ("omni_", "onsite_conversion.", "onsite_web_app_",
                     "onsite_web_", "onsite_",
                     "offsite_conversion.fb_pixel_", "offsite_conversion.",
                     "app_custom_event.", "offsite_")

_CUSTOM_RE = re.compile(r"^offsite_conversion\.custom\.(\d+)$")


@dataclass
class Event:
    """One conversion the account actually produced, after de-duplication."""
    key: str
    label: str
    one: str          # singular, for "cost per ___"
    kind: str
    aliases: list[str] = field(default_factory=list)
    ads: int = 0
    total: float = 0.0
    value: float = 0.0

    @property
    def has_value(self) -> bool:
        """Whether this event carries revenue, which is what makes ROAS real."""
        return self.value > 0

    def to_json(self) -> dict[str, Any]:
        return {"k": self.key, "lab": self.label, "one": self.one,
                "kind": self.kind,
                "ads": self.ads, "n": round(self.total, 2),
                "v": round(self.value, 2), "hv": self.has_value,
                # Shown in the picker's tooltip. The reader is choosing a
                # denominator for a cost target; which raw Meta surfaces it
                # covers is the thing that makes the number auditable.
                "al": self.aliases[:8]}


def _classify(action_type: str) -> tuple[str, str, str]:
    """(key, label, kind) for an action type with no known family."""
    if action_type in _ENGAGEMENT_EXACT or action_type.startswith(_ENGAGEMENT_PREFIX):
        return action_type, _humanize(action_type), "engagement"
    m = _CUSTOM_RE.match(action_type)
    if m:
        # An advertiser-defined custom conversion. The id is meaningless on its
        # own but is the only handle Meta gives, and the account's own naming
        # lives in Events Manager — so name it honestly rather than inventing.
        return action_type, f"Custom conversion {m.group(1)}", "other"
    if "messaging" in action_type:
        return action_type, _humanize(action_type), "message"
    if "call" in action_type:
        return action_type, _humanize(action_type), "call"
    if "lead" in action_type:
        return action_type, _humanize(action_type), "lead"
    if action_type.startswith("app_custom_event."):
        return action_type, _humanize(action_type), "app"
    # Offline/CRM-uploaded conversions arrive on their own surface and are kept
    # separate from the web event deliberately — they are the in-store half, and
    # merging them would hide the online/offline split rather than report it.
    if "purchase" in action_type:
        return action_type, _humanize(action_type), "purchase"
    if "registration" in action_type or "subscribe" in action_type:
        return action_type, _humanize(action_type), "signup"
    return action_type, _humanize(action_type), "other"


def _singular(label: str) -> str:
    """Fallback for an event with no named family — the trailing-s rule."""
    low = label.lower()
    return low[:-1] if low.endswith("s") and not low.endswith("ss") else low


def _humanize(action_type: str) -> str:
    s = action_type
    for p in _SURFACE_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    s = s.replace("fb_mobile_", "").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else action_type


def _canonical_score(action_type: str) -> tuple[int, int]:
    """Rank candidate names within a value-identical group; higher wins.

    Names are not interchangeable even when the numbers are. Left to a plain
    "shortest name" rule, Spectrum's group of seven 737s would be labelled
    `offsite_purchase_add_20_s_calls` — 30 characters, and a phone call
    presented as a purchase. So conversion-set and `*_grouped` aliases are
    pushed below everything else, and a known family name outranks all of it.
    """
    score = 0
    if action_type in _ALIAS_TO_FAMILY:
        score += 100
    if _CONV_SET_RE.search(action_type):
        score -= 50
    if "grouped" in action_type:
        score -= 30
    if action_type.startswith(_SURFACE_PREFIXES):
        score -= 10
    # Shorter is the tie-break, expressed as a second sort key.
    return score, -len(action_type)


def build_catalog(
    rows: Iterable[dict],
    *,
    min_ads: int = 2,
    limit: int = 16,
) -> list[Event]:
    """De-duplicated conversion events across a set of `ad_performance` rows.

    `rows` need only carry `platform_ad_id` and `extra_json`. Events firing on
    fewer than `min_ads` ads are dropped: a bucket comparison needs at least two
    ads before a cost-per-conversion means anything, and the tail of Meta's
    action list is mostly single-ad noise.
    """
    counts: dict[str, dict[str, float]] = defaultdict(dict)
    values: dict[str, float] = defaultdict(float)
    for r in rows:
        ad_id = r.get("platform_ad_id")
        if not ad_id:
            continue
        try:
            blob = json.loads(r.get("extra_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for a in blob.get("actions") or []:
            t, v = a.get("action_type"), _f(a.get("value"))
            if t and v:
                counts[t][ad_id] = counts[t].get(ad_id, 0.0) + v
        for a in blob.get("action_values") or []:
            t, v = a.get("action_type"), _f(a.get("value"))
            if t and v:
                values[t] += v

    # Pass 0 — drop conversion-set aliases outright.
    #
    # Value-identity catches the ones that duplicate exactly, but a set also
    # re-reports members whose totals drift slightly from the base event
    # (`offsite_content_view_add_meta_leads` 81,919,633 against `view_content`
    # 82,888,880), and those survive both passes. They are still duplicates,
    # and their names are the worst in the response: on Spectrum,
    # `offsite_purchase_add_20_s_calls` counts phone calls on an account with
    # no purchases at all. A set never reports a conversion its base event does
    # not, so nothing is lost by dropping the whole shape.
    considered = [t for t in counts if not _CONV_SET_RE.search(t)]

    # Pass 1 — collapse by known family name.
    fam_members: dict[str, list[str]] = defaultdict(list)
    loose: list[str] = []
    for t in considered:
        fam = _ALIAS_TO_FAMILY.get(t)
        (fam_members[fam[0]] if fam else loose).append(t)

    # Pass 2 — collapse what is left by per-ad value identity.
    by_vector: dict[tuple, list[str]] = defaultdict(list)
    for t in loose:
        by_vector[tuple(sorted(counts[t].items()))].append(t)

    events: list[Event] = []

    for key, label, one, kind, _aliases in _FAMILIES:
        members = fam_members.get(key)
        if not members:
            continue
        events.append(_merge(key, label, one, kind, members, counts, values))

    for members in by_vector.values():
        best = max(members, key=_canonical_score)
        key, label, kind = _classify(best)
        events.append(_merge(key, label, _singular(label), kind,
                             members, counts, values))

    # Engagement is classified rather than ignored so that a page like is
    # positively identified as a non-outcome, and then dropped here: the
    # catalogue's only job is to offer things a cost target can sit against.
    events = [e for e in events
              if e.kind != "engagement" and e.ads >= min_ads and e.total > 0]
    # Decision-relevant kinds first, then by how much of the account they cover.
    events.sort(key=lambda e: (KIND_ORDER.index(e.kind) if e.kind in KIND_ORDER
                               else len(KIND_ORDER), -e.ads, -e.total))
    return events[:limit]


def _merge(key: str, label: str, one: str, kind: str, members: list[str],
           counts: dict[str, dict[str, float]],
           values: dict[str, float]) -> Event:
    """Fold an alias group into one event.

    Takes the FIRST-listed member's numbers rather than summing, matching
    `meta_account._action_value`: the members are the same conversion reported
    on different surfaces, so adding them would count one lead up to four times.
    Members are ordered by canonical score, so the surface that wins the name
    also supplies the number — the count always matches its label.
    """
    ordered = sorted(members, key=_canonical_score, reverse=True)
    primary = ordered[0]
    per_ad = counts[primary]
    return Event(
        key=key, label=label, one=one, kind=kind, aliases=ordered,
        ads=len(per_ad), total=sum(per_ad.values()),
        value=values.get(primary, 0.0),
    )


def per_ad_counts(
    rows: Iterable[dict],
    catalog: list[Event],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """(counts, values) keyed by ad id then event key, zeros omitted.

    Reads the same primary alias `build_catalog` counted, so a per-ad number
    always rolls up to the catalogue total.
    """
    primary = {e.aliases[0]: e.key for e in catalog if e.aliases}
    out: dict[str, dict[str, float]] = defaultdict(dict)
    vals: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        ad_id = r.get("platform_ad_id")
        if not ad_id:
            continue
        try:
            blob = json.loads(r.get("extra_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for a in blob.get("actions") or []:
            k = primary.get(a.get("action_type"))
            v = _f(a.get("value"))
            if k and v:
                out[ad_id][k] = out[ad_id].get(k, 0.0) + v
        for a in blob.get("action_values") or []:
            k = primary.get(a.get("action_type"))
            v = _f(a.get("value"))
            if k and v:
                vals[ad_id][k] = vals[ad_id].get(k, 0.0) + v
    return out, vals


# Sentinel for "the purchases/revenue columns as ingested". Selecting it makes
# the dashboard read exactly the fields it always read, which is what keeps a
# commerce account's output unchanged by this feature — and it is the only
# choice for which attribution re-weighting exists, since `attribution_json`
# breaks out purchases and revenue but not arbitrary events.
PURCHASE_SENTINEL = "__pu"


def default_event(catalog: list[Event], *, has_revenue: bool) -> str:
    """Which event the dashboard opens on.

    Revenue settles it: an account reporting revenue is measured on ROAS, and
    anything else would change what every existing dashboard shows. Otherwise
    the highest-priority kind present wins, broken by ad coverage — which on
    Spectrum picks `lead` (67 ads) over `call_placed` (12 ads), matching the
    `OFFSITE_CONVERSIONS` goal 46 of its adsets are optimised for.
    """
    if has_revenue:
        return PURCHASE_SENTINEL
    for kind in KIND_ORDER:
        if kind == "engagement":
            break
        picks = [e for e in catalog if e.kind == kind]
        if picks:
            return max(picks, key=lambda e: (e.ads, e.total)).key
    return PURCHASE_SENTINEL


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
