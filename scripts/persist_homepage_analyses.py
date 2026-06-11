#!/usr/bin/env python
"""Persist hand-authored homepage site-content analyses to homepage_promos.

Authored inline (the multi-modal LLM workflow hung at scale). Each analysis is
grounded in a direct read of the brand's landing_hero.png + context bundle.

Run once per deployment:
    INTEL_DB_PATH=data/intel.db .venv/bin/python scripts/persist_homepage_analyses.py bobs
    INTEL_DB_PATH=data/trex.db .venv/bin/python scripts/persist_homepage_analyses.py trex
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


# Each entry includes: observation_id, source_id, observed_at (so we can call
# record_homepage_promo cleanly), plus the full analysis dict.
ANALYSES = {
    "bobs": [
        {
            "competitor_id": "bobs",
            "observation_id": 27, "source_id": 1,
            "observed_at": "2026-06-04T22:25:13+00:00",
            "analysis": {
                "headline": "Top-rated AND affordable",
                "subhead": "Treasure® boxed queen firm mattress — $799",
                "primary_cta_text": "Shop now",
                "primary_cta_url": None,
                "secondary_ctas": ["Shop all mattresses", "Financing options", "Find a store", "Chat"],
                "offer_claim": "Consumer Reports Recommended at $799",
                "offer_value": 799,
                "offer_kind": "price_anchor",
                "expiration": None,
                "channel_callouts": ["Flexible financing", "Deliver-to zip integration", "Nearest store finder", "Live chat", "860-812-1111 shop-via-phone line"],
                "urgency_cues": [],
                "positioning_statement": "Bob's owns the value-with-validation wedge unambiguously — the hero pairs a Consumer Reports Recommended badge with a specific $799 mattress price and the conjunction 'AND' doing strategic work between 'Top-rated' and 'affordable.' This is the clearest single positioning statement of any homepage in the Bobs competitive set, and it's executed in the hero, not buried in a brand-story page.",
                "messaging_stance": {
                    "rational_vs_emotional": "rational",
                    "value_vs_premium_signaling": "mid",
                    "lead_with": "social_proof",
                    "tone_one_word": "warm-rational"
                },
                "design_critique": {
                    "hero_layout": "Wide warm-lit bedroom scene with quilted mattress center-frame, Consumer Reports Recommended badge overlay in white card, large yellow Shop Now button — the badge does heavier work than the headline. Eye lands on the badge first, then the price, then the CTA.",
                    "visual_consistency": "Coherent. Yellow CTA color matches the Bob's brand red+yellow system; warm imagery extends to the below-the-fold financing banner with the brand's character holding piggy banks. Consumer Reports badge integrates without feeling foreign.",
                    "cta_clarity": "Strong single primary action (yellow Shop now), with a secondary text link (Shop all mattresses). The hero CTA hierarchy is clean — most legible in the Bobs deck.",
                    "use_of_imagery": "Studio-lit product-in-room scene; no people in the hero but warmth implied through soft daylight and styled nightstand. Below the fold flips to flat category icons (text-heavy grid) — a tonal break that loses some of the warmth.",
                    "notable_design_choices": [
                        "Consumer Reports badge above the price is a credibility moat — only brand in the set borrowing third-party validation in the hero",
                        "Financing banner uses Bob's mascot energy (man with piggy bank) — most personality-driven below-the-fold module in the set",
                        "Deliver-to zip code in utility nav — implicit logistics promise, no other set member surfaces this"
                    ]
                },
                "products_pushed": ["Treasure® boxed queen firm mattress ($799)", "Mattresses (category)", "Top Rated tagged products (Canyon bedroom collection)"],
                "audience_signals": [
                    "Cost-conscious quality shoppers (Consumer Reports stamp + $799 price)",
                    "First-time / young-family buyers (financing banner with friendly mascot energy)",
                    "Brand-trusted repeat customers (Top Rated section + 4.7-4.8 star products with UGC reviews captioned by 'mybobs')"
                ],
                "what_this_homepage_does_well": [
                    "Names the wedge explicitly — 'Top-rated AND affordable' is the clearest single positioning sentence in the entire competitive set, with a Consumer Reports badge proving it in the same viewport",
                    "Brand voice extends into product UGC ('Bob, unplugged 😎', 'Did I make this Modular Bob® dog bed because it's CUTE?!') — the Bobs mascot personality is alive in customer review captions, not just in legacy ads",
                    "Financing call-out is warm rather than transactional ('Why wait? Bob's has flexible financing!') — softens what is usually the most utilitarian module on furniture sites"
                ],
                "what_it_misses": [
                    "Zero urgency or time-bound offer — the $799 mattress is evergreen, no Father's Day or July 4 anchor, no end-date",
                    "Bob himself is absent from the hero — the mascot lives in the financing banner and review UGC but not the lead viewport, missing the most distinctive brand asset",
                    "The four simultaneous new-store opening flights running in paid Meta (Pineville, Spartanburg, University City, Greenville) don't appear on the homepage — owned channel and paid channel aren't telling the same growth story"
                ],
                "strategist_one_liner": "Bob's homepage is the rare furniture page that names its wedge in the hero — 'Top-rated AND affordable' with a Consumer Reports badge proving it — but it leaves the regional expansion story and the namesake mascot out of the lead viewport.",
                "confidence": 0.91
            }
        },
        {
            "competitor_id": "american_freight",
            "observation_id": 29, "source_id": 11,
            "observed_at": "2026-06-04T22:25:54+00:00",
            "analysis": {
                "headline": "Mattresses STARTING AT ONLY $49.99",
                "subhead": None,
                "primary_cta_text": "SHOP MATTRESSES",
                "primary_cta_url": None,
                "secondary_ctas": ["SEE THE 15 PC SET", "SHOP BEDS", "Find a Store", "Shop today and save!", "Clearance"],
                "offer_claim": "Mattresses from $49.99 · $999 Whole Home Package · Queen Beds from $149.99 · Every Day Hot Deals",
                "offer_value": 49.99,
                "offer_kind": "price_anchor",
                "expiration": None,
                "channel_callouts": ["Find A Store", "Location-aware (Dothan, AL · Open Today 11AM-7PM)", "Shop by Size/Type/Comfort mattress finder"],
                "urgency_cues": ["Every Day Hot Deals", "Shop today and save!"],
                "positioning_statement": "American Freight runs the deepest-value page in the entire competitive set — the hero is essentially a digital newspaper inserter ad. Multiple stacked price-point banners ($49.99, $999, $149.99) compete in the first viewport, and the brand identity sits in the price points themselves rather than in any tagline or imagery. This is positioning by repetition: lowest price, communicated relentlessly.",
                "messaging_stance": {
                    "rational_vs_emotional": "rational",
                    "value_vs_premium_signaling": "value",
                    "lead_with": "offer",
                    "tone_one_word": "transactional"
                },
                "design_critique": {
                    "hero_layout": "Red-on-white headline 'Mattresses STARTING AT ONLY $49.99' over a small studio-lit bedroom scene, dominant red 'SHOP MATTRESSES' pill button. The image is small relative to the typography — the price is the protagonist, not the product.",
                    "visual_consistency": "Loud and consistent within itself — the red/blue/green/yellow promo-banner palette runs through every module, but the overall effect is closer to a retailer flyer than a brand system. Few visual quiet moments.",
                    "cta_clarity": "Every module has a CTA, which paradoxically dilutes the primary one. The first viewport contains at minimum 5 distinct CTAs (Shop today, Shop Mattresses, See 15 pc Set, Shop Beds, plus utility nav).",
                    "use_of_imagery": "Predominantly studio product shots on white — flat, catalog-style. The exception is the small bedroom lifestyle inset under the hero headline. Photography quality is a clear differentiator vs Raymour or Bobs.",
                    "notable_design_choices": [
                        "Location-aware in utility nav ('Dothan, AL · Open Today 11AM-7PM') — operational warmth in an otherwise transactional page",
                        "Welcome message reads 'Welcome to our fashion store!' in body text — likely CMS template copy, suggests homepage quality control is uneven",
                        "Red 'Every Day Hot Deals' banner anchors urgency without naming an end date — urgency-as-default rather than urgency-with-occasion"
                    ]
                },
                "products_pushed": ["Mattresses ($49.99+)", "$999 Whole Home Package", "Queen Beds ($149.99+)", "Sofas", "Reclining Furniture", "Bedroom Sets"],
                "audience_signals": [
                    "Cash-tight cross-shoppers (lowest visible price point in the set)",
                    "Credit-constrained buyers (financing/layaway language in paid Meta)",
                    "Local store visitors (Find a Store + zip-aware hours)"
                ],
                "what_this_homepage_does_well": [
                    "Owns the lowest price-points visibly — $49.99 mattress and $99 mattress are below anything any other set member shows in their hero",
                    "Whole Home Package framing ($999 for everything) is competitively unique — addresses move-in / relocation buyer differently than competitors' room-by-room model",
                    "Location-aware top nav with store hours is operationally helpful for the audience this serves"
                ],
                "what_it_misses": [
                    "The tax-refund 'Make Every Tax Dollar Count' framing running in paid Meta in JUNE is also absent here — paid and owned are stale together",
                    "No financing-led messaging in the hero despite financing being the brand's competitive moat for this audience (the paid Meta 'Free Layaway / $50 down' lines don't appear)",
                    "Stock 'Welcome to our fashion store!' template copy in the body text suggests low homepage editorial discipline",
                    "Zero distinctive brand asset — no mascot, no signature color treatment beyond generic red, no equity-building tagline"
                ],
                "strategist_one_liner": "American Freight runs the cheapest, loudest homepage in the set — multiple price points stacked, every module a CTA — but it borrows zero equity from the financing/layaway moat that defines its paid-Meta positioning.",
                "confidence": 0.87
            }
        },
        {
            "competitor_id": "raymour_flanigan",
            "observation_id": 28, "source_id": 7,
            "observed_at": "2026-06-04T22:25:43+00:00",
            "analysis": {
                "headline": "STACK YOUR SAVINGS — 20% off purchases $4,999+",
                "subhead": "15% off purchases $3,499+ · 10% off purchases $1,499–$3,499",
                "primary_cta_text": "Shop Now",
                "primary_cta_url": None,
                "secondary_ctas": ["Find My Store", "Free Design Consultation", "Live Chat", "Need Help Shopping? Call 1-888-729-6687", "Business to Business"],
                "offer_claim": "Tiered: 20% / 15% / 10% off scaling with purchase value · Mattresses as low as $15/mo for 72 months ($1,049 total) · Sofas from $399 in stock, 3-day delivery",
                "offer_value": 20,
                "offer_kind": "tiered_percent_off",
                "expiration": None,
                "channel_callouts": ["Free Design Consultation", "Business to Business", "3D Tools", "Live Chat", "1-888-729-6687 phone line", "Open store hours surfaced ('Open Until 8pm Jamestown')", "3 days or less in-stock delivery"],
                "urgency_cues": [],
                "positioning_statement": "Raymour runs the most operationally sophisticated homepage in the set — tiered savings, in-stock delivery promise (3 days), 72-month financing math made explicit ($15/mo × 72 = $1,049), service channels surfaced everywhere (chat, phone, design consult). The brand is positioning itself as the most service-rich operator, with savings math that rewards bigger baskets. Not warm, but competent.",
                "messaging_stance": {
                    "rational_vs_emotional": "rational",
                    "value_vs_premium_signaling": "mid",
                    "lead_with": "offer",
                    "tone_one_word": "operational"
                },
                "design_critique": {
                    "hero_layout": "Purple tier-discount panel left + lifestyle living room shot center + 'sofas AS LOW AS $399' panel right. Three offers competing for attention — the eye gets pulled multiple directions before landing.",
                    "visual_consistency": "Strong purple brand color throughout. The financing module below the hero ('MATTRESSES AS LOW AS $15/mo') is the most visually disciplined element on the page — clean type-led design that out-shines the busy hero.",
                    "cta_clarity": "Two primary purple 'Shop Now' buttons competing in the hero viewport. Plus Live Chat + phone number + Find My Store in utility — clear but crowded.",
                    "use_of_imagery": "Lifestyle living-room shot in the hero (Starlight Triple Power Living Room Set in a styled scene), product-isolated below the fold. Better photo discipline than American Freight, less aspirational than TimberTech.",
                    "notable_design_choices": [
                        "Tiered discount with explicit $ thresholds — financial reasoning treated as a feature, not hidden in fine print",
                        "'In stock & delivered in 3 days or less' is a specific operational promise no peer matches in their hero",
                        "Financing math made explicit ($15/mo × 72 = $1,049 total, with 'in-store only with your Raymour & Flanigan Credit Account') — radical transparency on consumer credit"
                    ]
                },
                "products_pushed": ["Starlight Triple Power Living Room Set", "Whitman Sofa ($399)", "Mattresses ($15/mo financing)", "Drew and Jonathan Home collection"],
                "audience_signals": [
                    "Higher-ticket renovators (20% off triggers at $4,999+)",
                    "Financing-aware buyers (explicit monthly + total cost shown)",
                    "Speed-of-delivery buyers (3-day promise on in-stock)",
                    "B2B customers (Business to Business utility-nav entry)"
                ],
                "what_this_homepage_does_well": [
                    "Tiered discount math is a sophisticated lever — incentivizes bigger baskets without doing a flat blanket promotion",
                    "3-day delivery promise on in-stock is the operational moat the brand is building, surfaced in the hero rather than buried in shipping FAQ",
                    "Free Design Consultation as a utility-nav entry treats the design phase as a service the brand provides — same upstream-audience play as TimberTech's 'For Architects' nav"
                ],
                "what_it_misses": [
                    "Three offers competing in the hero dilute each other — a single dated tentpole would out-perform the stacked tiers",
                    "The '80 years' trust anchor running in paid Meta isn't in the hero — heritage equity sits idle on the page",
                    "No emotional creative — every module is rational/operational. The 'Better Sleep Begins Here' collection spotlight is the closest to feeling, and it lives below three offers"
                ],
                "strategist_one_liner": "Raymour runs the most operationally sophisticated homepage in the Bobs set — tiered savings, explicit financing math, 3-day delivery promise — but the cognitive load of three competing hero offers undermines what individual elements do well.",
                "confidence": 0.90
            }
        },
        {
            "competitor_id": "rooms_to_go",
            "observation_id": 30, "source_id": 5,
            "observed_at": "2026-06-04T22:32:06+00:00",
            "analysis": {
                "headline": "SHOP, DECORATE & SAVE + FREE SHIPPING",
                "subhead": "We ship accents, décor & more almost everywhere so you can give any space a personal touch.",
                "primary_cta_text": "SHOP NOW",
                "primary_cta_url": None,
                "secondary_ctas": ["See More (category tiles)", "Find out where we offer full service furniture delivery — LEARN MORE", "Find a furniture showroom near you"],
                "offer_claim": "FREE SHIPPING BY ITEM on accents, décor and more",
                "offer_value": None,
                "offer_kind": "free_shipping",
                "expiration": None,
                "channel_callouts": ["FREE SHIPPING BY ITEM banner", "Full service furniture delivery (region-gated)", "150+ showrooms across 10 states (AL, FL, GA, LA, MS, NC, SC, TN, TX, VA)", "Rooms To Go Business Plus", "Family-owned since 1990 trust signal"],
                "urgency_cues": [],
                "positioning_statement": "Rooms To Go is leaning hard into a free-shipping + complete-room-package positioning. The hero is split: a navy-overlay card states the offer flatly ('SHOP, DECORATE & SAVE + FREE SHIPPING') flanked by lifestyle accent-furniture imagery. Below the fold the page becomes essentially a brand-history essay ('Family-Owned Since 1990', 'Complete Room Packages — Fast and Affordable') — unusual for a furniture homepage, suggests SEO/E-E-A-T optimization is driving content layout.",
                "messaging_stance": {
                    "rational_vs_emotional": "rational",
                    "value_vs_premium_signaling": "value",
                    "lead_with": "offer",
                    "tone_one_word": "homespun"
                },
                "design_critique": {
                    "hero_layout": "Three-pane composition — left lifestyle (chair + cabinet), center navy offer card, right lifestyle (storage piece). The center card carries the whole message; the lifestyle wings provide texture but no separate story.",
                    "visual_consistency": "Clean. Navy brand color across hero card, category tile chips, secondary CTAs. The 'ROOMS TO GO' logo with stacked teal accent is competitive distinctiveness in the set.",
                    "cta_clarity": "Single white-on-navy Shop Now button in the hero is unambiguous. Category circle-tiles below provide soft secondary navigation without competing visually.",
                    "use_of_imagery": "Studio-styled lifestyle (well-lit, sparse, editorial). Below the fold flips to flat product circle-tiles — coherent design system.",
                    "notable_design_choices": [
                        "Body content below the fold is a brand-history essay rather than product modules — extreme SEO play (likely driven by the 'family-owned since 1990' E-E-A-T story Google rewards)",
                        "Rooms To Go Business Plus surfaced as a callout — most-explicit B2B-aware furniture homepage in the set after Raymour",
                        "Category circle-tiles with item counts ('263 items') are an unusual UX choice — surfaces breadth as a value prop"
                    ]
                },
                "products_pushed": ["Accents, décor, and ship-direct items", "Complete Room Packages", "Living/Bedroom/Dining categories"],
                "audience_signals": [
                    "Out-of-region shoppers who can't visit showrooms (free shipping framing)",
                    "Move-in / first-home buyers (complete room packages)",
                    "Multi-property investors (Business Plus callout)",
                    "Southeast and Texas buyers (showroom footprint stated explicitly)"
                ],
                "what_this_homepage_does_well": [
                    "Brand-history paragraph below the fold is a tasteful trust play — most furniture retailers hide the founding story; Rooms To Go puts it on the homepage",
                    "Free Shipping By Item is a clean differentiator — distinct from American Signature's 'minimum order' standard, communicated upfront",
                    "Region-aware delivery copy ('Find out where we offer full service furniture delivery') is honest about geographic limits rather than hiding them"
                ],
                "what_it_misses": [
                    "Hero is generic — 'SHOP, DECORATE & SAVE' is functional but not differentiated. Nobody in the set is running urgent dated offers, so this doesn't suffer competitively, but it doesn't surprise either",
                    "The complete-room-package story ('buy the piece, save a little; buy the room, save a lot') is buried in the brand essay below the fold — strong proposition, poor placement",
                    "Paid Meta is being impersonated (Novels Lover / New world publications running ads under the RTG page name) — owned channel is healthy, paid channel needs brand-protection work"
                ],
                "strategist_one_liner": "Rooms To Go runs a quietly competent homepage with a brand-history essay below the fold — the strongest E-E-A-T trust play in the set — but the complete-room-package proposition that's actually their commercial differentiator is buried while a generic free-shipping line takes the hero.",
                "confidence": 0.85
            }
        },
        {
            "competitor_id": "value_city",
            "observation_id": 31, "source_id": 9,
            "observed_at": "2026-06-04T22:32:13+00:00",
            "analysis": {
                "headline": "All American Signature Furniture and Value City Furniture locations are closed.",
                "subhead": None,
                "primary_cta_text": "Track Your Order",
                "primary_cta_url": None,
                "secondary_ctas": ["Frequently Asked Questions", "https://www.veritaglobal.net/americansignature (Chapter 11 claims agent)"],
                "offer_claim": None,
                "offer_value": None,
                "offer_kind": None,
                "expiration": None,
                "channel_callouts": ["Veritage Global claims-agent portal", "Phone lines for claims: (877) 726-6511 / +1 (424) 236-7251"],
                "urgency_cues": [],
                "positioning_statement": "Value City Furniture is OUT OF BUSINESS. The homepage as captured is a Chapter 11 wind-down notice — every store closed, no returns or exchanges accepted, no gift cards honored, deposits handled via a third-party claims agent (Verita). This is not a scrape miss or a stale page; this is the current state of the brand. American Signature Inc. (the parent) has filed Chapter 11. Value City has effectively exited the competitive set.",
                "messaging_stance": {
                    "rational_vs_emotional": "rational",
                    "value_vs_premium_signaling": "n/a",
                    "lead_with": "service",
                    "tone_one_word": "shuttered"
                },
                "design_critique": {
                    "hero_layout": "Plain text crisis notice on white. Combined Value City Furniture + American Signature Furniture logo lockup at top, single-line headline announcing closure, FAQ accordion as the body.",
                    "visual_consistency": "Functional only — no brand imagery, no marketing colors, no lifestyle photography. Designed for legal/operational clarity, not commerce.",
                    "cta_clarity": "Single useful action ('Track Your Order'). Otherwise the page redirects everything to the Verita claims-agent portal.",
                    "use_of_imagery": "None. The brand logo lockup is the only graphic.",
                    "notable_design_choices": [
                        "Combined Value City + American Signature logo at top — parent and consumer-facing brands unified in the shutdown notice",
                        "FAQ-driven layout — every question routes to the Verita Global claims-agent URL",
                        "Copyright 2025 American Signature, Inc. at the footer — page has not been touched since the bankruptcy notice was posted"
                    ]
                },
                "products_pushed": [],
                "audience_signals": [
                    "Existing customers with open orders or deposits (Track Your Order, Verita claims)",
                    "Financing-account holders ('Yes, customers should continue to make their financing payments as usual')",
                    "Gift-card holders (no longer redeemable)"
                ],
                "what_this_homepage_does_well": [
                    "Clearly states closure status without burying it — material legal compliance",
                    "Routes claims through a single third-party authority (Verita Global) rather than asking customers to self-navigate",
                    "Acknowledges open commitments (financing, deposits) rather than going silent"
                ],
                "what_it_misses": [
                    "Strategically: this brand is GONE. The 'competitive set' for Bob's now has 6 active brands, not 7. Any briefing, dashboard, or strategy doc that treats Value City as a live competitor is wrong",
                    "There is no consumer-facing brand to analyze — positioning, messaging, design critique are all moot",
                    "Bob's mid-Atlantic regions where Value City previously competed are now uncontested by this brand — a real market-share opportunity worth quantifying"
                ],
                "strategist_one_liner": "Value City Furniture is out of business — the homepage is a Chapter 11 closure notice. Bob's competitive set should be redrawn around this; the geographies Value City vacated are unclaimed share for whoever moves first.",
                "confidence": 0.99
            }
        },
    ],
    "trex": [
        {
            "competitor_id": "trex",
            "observation_id": 1, "source_id": 1,
            "observed_at": "2026-06-08T17:17:31+00:00",
            "analysis": {
                "headline": "Meet the Color Defining 2026",
                "subhead": "Biscayne brings driftwood tones and quiet sophistication to your outdoor space.",
                "primary_cta_text": "Shop Samples",
                "primary_cta_url": None,
                "secondary_ctas": ["See All Shades", "Get a Quote", "Order Samples", "Order up to 5 FREE decking samples"],
                "offer_claim": "Order up to 5 FREE decking samples",
                "offer_value": 5,
                "offer_kind": "free_sample",
                "expiration": None,
                "channel_callouts": ["TrexPro® Contractor network", "Find a TrexPro® Contractor Near You", "Cost Calculator", "Color Selector", "AI Deck Visualizer (Beta)", "Where to Buy locator", "For Professionals nav"],
                "urgency_cues": [],
                "positioning_statement": "Trex runs the category-leader playbook explicitly — leading with the '2026 Color of the Year' (Biscayne) is borrowing the Pantone playbook to make a SKU launch culturally meaningful. The hero is aesthetic-first (a person enjoying the deck, not the deck itself); the buying-funnel infrastructure (free samples, TrexPro contractors, calculator, visualizer) sits underneath as a complete journey-scaffold. This is positioning as the brand that defines what good decking looks like.",
                "messaging_stance": {
                    "rational_vs_emotional": "mixed",
                    "value_vs_premium_signaling": "premium",
                    "lead_with": "brand_story",
                    "tone_one_word": "aspirational"
                },
                "design_critique": {
                    "hero_layout": "Slider hero — a person from above on a multi-color deck (towels in coral/teal/cream), large white headline overlay, chartreuse 'Shop Samples' primary CTA + outline 'See All Shades' secondary. The deck-boards themselves are the visual floor, not the subject.",
                    "visual_consistency": "Strong. Forest-green + chartreuse threads from the announcement bar through the utility nav (Order Samples) and into the hero CTA. The brand system is one of the most distinctive in any home-improvement category.",
                    "cta_clarity": "Clean two-button hierarchy. Free-sample offer is surfaced THREE times in the first viewport (announcement bar, hero CTA, utility nav) — friction-minimized funnel.",
                    "use_of_imagery": "Lifestyle, not product. The composition prioritizes the person and the moment; product detail is reserved for below-the-fold proof modules (SunComfortable, golden retriever, technology rows).",
                    "notable_design_choices": [
                        "Color of the Year framing — borrows the Pantone playbook to make a SKU launch culturally meaningful",
                        "Free sample limit ('up to 5') stated as a benefit, not a constraint — generous-feeling, removes friction",
                        "Chartreuse + forest-green is one of the only distinctive composite-decking brand systems in market — instantly recognizable"
                    ]
                },
                "products_pushed": ["Trex Select 2026 colors (Biscayne anchor)", "Railing Visualizer", "Decking Samples", "SunComfortable Technology"],
                "audience_signals": [
                    "Homeowners researching a deck project (lifestyle hero, free-sample funnel)",
                    "Style-led shoppers (Color of the Year is a fashion-cycle frame)",
                    "Pros via 'For Professionals' utility nav (segmented entry)",
                    "DIY-curious via 'Learn to DIY' and 'DIY with Trex Academy'"
                ],
                "what_this_homepage_does_well": [
                    "Leads with brand-equity moment (Color of the Year) rather than promo — appropriate for a category-leader with a 6-18 month buying cycle; Ehrenberg-Bass would call this mental-availability-first marketing",
                    "Free-sample offer placed at every entry point (announcement bar, hero CTA, utility nav) — the lowest-friction conversion funnel of any brand in the decking set",
                    "Pro/consumer segmentation is clean — 'For Professionals' has its own utility entry without diluting the consumer hero",
                    "AI Deck Visualizer (Beta) is the only AI-tool callout in the set — signals product-team investment"
                ],
                "what_it_misses": [
                    "Zero urgency — no end date on the Color of the Year campaign, no scarcity, no seasonal tentpole framing in early deck-build season",
                    "The hero is beautiful but says nothing about the actual product differentiation (Refuge fire-resistant, marine-grade, recycled content) that paid Meta IS pushing — owned and paid channels are out of sync",
                    "No US-Spanish language option in nav (Deckorators offers FR; nobody offers Spanish despite TX/FL/CA being core deck markets with Hispanic majority counties)",
                    "Below-the-fold SunComfortable spec card is competing visually with the dog photo — the differentiation moment lacks emphasis"
                ],
                "strategist_one_liner": "Trex's homepage runs the category-leader playbook properly — aesthetic moment in the hero, free samples threaded everywhere, segmented pro entry — but every differentiation moat the brand is building in paid Meta (Refuge, marine, recycled) is left out of the owned-channel story.",
                "confidence": 0.90
            }
        },
        {
            "competitor_id": "timbertech",
            "observation_id": 10, "source_id": 3,
            "observed_at": "2026-06-08T20:57:16+00:00",
            "analysis": {
                "headline": "The beauty of wood without the upkeep",
                "subhead": None,
                "primary_cta_text": "Start Your Deck Project",
                "primary_cta_url": None,
                "secondary_ctas": ["Order Free Samples", "Find a Contractor", "Request a Quote", "For Pros", "For Architects", "Explore the Technology"],
                "offer_claim": "Order Free Samples",
                "offer_value": None,
                "offer_kind": "free_sample",
                "expiration": None,
                "channel_callouts": ["Find a Contractor", "Find a Showroom", "Talk to an Expert", "3D Deck Designer", "Outdoor Living Sourcebook", "Decking Cost Calculator", "Chat with us"],
                "urgency_cues": [],
                "positioning_statement": "TimberTech positions itself as the design-conscious alternative to natural wood — a single confident headline ('The beauty of wood without the upkeep') sits over an intimate styled corner (basket + plant on warm-tone decking) with a single dark-pill CTA. The secondary frame is service-led: 'Start Your Deck Project' implies hand-holding through the planning phase, reinforced by both Architect and Pro entries in utility nav.",
                "messaging_stance": {
                    "rational_vs_emotional": "emotional",
                    "value_vs_premium_signaling": "premium",
                    "lead_with": "brand_story",
                    "tone_one_word": "intimate"
                },
                "design_critique": {
                    "hero_layout": "Cropped close-up of styled deck corner — basket, plant, raking light across grain — overlaid by centered headline and a single dark-pill CTA. No people; the absence is conspicuous given the lifestyle framing.",
                    "visual_consistency": "Strong editorial system. Warm wood + cream + rust-orange accent (the Chat with us bubble is the brand's loudest pop of color). Serif logotype + warm palette carry the 'natural premium' positioning end-to-end.",
                    "cta_clarity": "Single dark pill in hero ('Start Your Deck Project') — confident minimalist choice, but downstream secondary actions (Find a Contractor, Request a Quote) are buried in utility nav rather than offered as second-level entries below the hero CTA.",
                    "use_of_imagery": "Heavily styled lifestyle. Below the fold a three-card row mixes flat-lay samples, an outdoor lifestyle scene, and a portrait of a smiling contractor — visual evidence of the brand's 'contractor authority' messaging from paid Meta.",
                    "notable_design_choices": [
                        "Architect-specific nav entry — only brand in the set explicitly segmenting design professionals from contractors",
                        "'Chat with us' rust-orange bubble is the loudest color on the page — service-led positioning made literal",
                        "Intentional absence of people in the hero — the deck itself is the protagonist",
                        "Single CTA discipline — most editorially confident hero in either deck"
                    ]
                },
                "products_pushed": ["Advanced PVC Decking", "Composite Decking", "Decking Collections", "Fire Resistance (in submenu)", "Multiwidth Decking"],
                "audience_signals": [
                    "Homeowners planning a deck project (Start Your Deck Project framing)",
                    "Architects (dedicated 'For Architects' nav — most upstream-design-pro courtship in the set)",
                    "Contractors ('For Pros' + 'Trusted by Pros' brand-build content)",
                    "Newsletter-driven buyer-stage segmentation (the email-capture form asks 'I am the... Architect / Builder / Contractor / Dealer / Designer / Homeowner / Installer / Landscaper / Other')"
                ],
                "what_this_homepage_does_well": [
                    "Single confident headline + single CTA hero — high editorial discipline that mirrors the brand-build paid-Meta tone",
                    "Architect-specific entry point is competitively unique — claiming the upstream specifier audience nobody else is courting",
                    "'The beauty of wood without the upkeep' is the cleanest single category-of-one positioning statement of any homepage in either competitive set",
                    "Newsletter form segments by role on capture — sophisticated lead-quality lever competitors aren't pulling"
                ],
                "what_it_misses": [
                    "Zero urgency / no time-bound offer — same gap as the rest of the set, but more conspicuous given the elegant brand system that could carry a tasteful tentpole",
                    "Fire Resistance is buried in the Decking submenu despite being a paid-Meta hero claim ('top-rated fire resistance') — owned channel under-leverages the differentiation paid is building",
                    "No people in hero — the contractor-testimonial engine that's the most distinctive thing in paid Meta isn't reflected in the homepage's visual choice",
                    "No Spanish language option in nav"
                ],
                "strategist_one_liner": "TimberTech runs the most editorially confident homepage in the set — a single line, a single button, a styled corner — and it's the only brand explicitly courting architects, but the fire-resistance and contractor-authority equity it's compounding in paid Meta is missing from the owned hero.",
                "confidence": 0.89
            }
        },
        {
            "competitor_id": "fiberon",
            "observation_id": 5, "source_id": 5,
            "observed_at": "2026-06-08T17:21:03+00:00",
            "analysis": {
                "headline": "Introducing NOVUS™ Composite Decking",
                "subhead": "Experience a story in every board with Novus™ fused composite decking. Each deck board features the captivating appearance of real wood with a slip-resistant surface that's engineered to last.",
                "primary_cta_text": "Explore Novus",
                "primary_cta_url": None,
                "secondary_ctas": ["Order Samples", "Find An Installer", "Find A Dealer", "Pro Center", "Explore Railing", "Explore Lighting", "Watch Now (Promenade)"],
                "offer_claim": None,
                "offer_value": None,
                "offer_kind": None,
                "expiration": None,
                "channel_callouts": ["Find An Installer", "Find A Dealer", "Pro Center", "Ascendant Login", "Deck Cost Calculator", "Discovery AR App", "Discovery Deck Designer", "Product Visualizer", "12+ language regional selector"],
                "urgency_cues": [],
                "positioning_statement": "Fiberon is positioning the homepage around a new product launch — Novus™ fused composite decking. The headline carries the product name rather than a positioning claim. Above the nav, a Fortune Brands sibling-logo strip (Therma-Tru Doors, LaMason, fiberon, Fypon, SolarInnovations) signals a corporate-portfolio play: the brand is communicating credibility-by-association rather than standalone identity. This is the homepage of a B2B-aware brand pretending to be a consumer brand.",
                "messaging_stance": {
                    "rational_vs_emotional": "rational",
                    "value_vs_premium_signaling": "premium",
                    "lead_with": "product",
                    "tone_one_word": "transactional"
                },
                "design_critique": {
                    "hero_layout": "High-angle styled deck shot in warm browns with centered product-launch headline and a red 'Explore Novus' CTA. Reads more like a product-launch landing page than a brand-equity hero.",
                    "visual_consistency": "Mid. The parent-brand sibling strip above the nav introduces visual noise from the Fortune Brands portfolio that no consumer-brand site should carry on hero. Inside the hero the system is clean — italic lowercase 'fiberon' wordmark, warm woods, red accent — but the relationship between Fiberon and its siblings is visually unresolved.",
                    "cta_clarity": "Single red 'Explore Novus' button — clear and high-contrast against the warm wood backdrop. Below-the-fold secondary CTAs (Explore Railing, Explore Lighting) follow a consistent treatment with overlay text + dark pill.",
                    "use_of_imagery": "Styled lifestyle imagery — high-angle deck close-ups, no people in hero. Below the fold rotating cards lean into emotional copy ('Make more time for memories') with twilight outdoor scenes.",
                    "notable_design_choices": [
                        "Parent-brand sibling strip across the top — unusual for a consumer site, signals B2B/specifier audience expectation",
                        "12+ language selector in nav — most aggressive internationalization in the decking set, hints at global B2B distribution",
                        "Italic lowercase 'fiberon' wordmark is the only non-uppercase brand mark in the competitive set — distinctive",
                        "Ascendant Pro login surfaced in utility nav — pro-portal as a first-class entry"
                    ]
                },
                "products_pushed": ["Novus™ Composite Decking (launch SKU)", "Promenade Decking", "Railing", "Lighting"],
                "audience_signals": [
                    "Specifiers and pros (parent-brand strip, multi-language nav, Ascendant Pro login front-and-center)",
                    "Homeowners as a secondary audience (Order Samples, Deck Cost Calculator)",
                    "Global / non-US markets (12+ language selector)"
                ],
                "what_this_homepage_does_well": [
                    "Product-launch hero is fast — a homeowner arriving from a Novus-promoted source lands directly on the matching message",
                    "Below-the-fold cards segment by adjacent product category (Railing, Lighting) without leaving the homepage — keeps the consumer in-funnel",
                    "Italic lowercase 'fiberon' wordmark is a real distinctive asset"
                ],
                "what_it_misses": [
                    "No active Meta advertising in market this window — homepage traffic is essentially un-supported by paid media (verified via ads_library_search)",
                    "The parent-brand sibling strip is consumer-noise — it serves B2B specifiers but confuses the homeowner buyer journey",
                    "No people in the hero, no testimonials in the first viewport — emotion is reserved for below-the-fold cards where it competes with product-launch noise",
                    "No urgency / no offer / no time-bound mechanism"
                ],
                "strategist_one_liner": "Fiberon is running a product-launch homepage (Novus™) with no paid-Meta wind in its sails — owned channel is doing the work alone, and the parent-brand portfolio strip creates brand-architecture noise the consumer can't decode.",
                "confidence": 0.86
            }
        },
        {
            "competitor_id": "deckorators",
            "observation_id": 12, "source_id": 7,
            "observed_at": "2026-06-08T20:58:20+00:00",
            "analysis": {
                "headline": "DESIGNED TO ENDURE. BUILT TO ENJOY.",
                "subhead": "Advanced decking and railing solutions engineered to outperform wear and tear, every time.",
                "primary_cta_text": "Explore Our Products",
                "primary_cta_url": None,
                "secondary_ctas": ["Shop Free Samples", "Shop Free Samples · Shipping Included", "Where to Buy", "Find a Builder", "Order Free Samples", "Connect with a Contractor", "Request a Catalog"],
                "offer_claim": "Free Samples · Shipping Included",
                "offer_value": None,
                "offer_kind": "free_sample",
                "expiration": None,
                "channel_callouts": ["Where to Buy", "Find a Builder", "Connect with a Contractor", "Deck Visualizer", "Deck Cost Calculator", "Deck Match Quiz", "Baluster Calculator", "3D Deck Builder", "Railing Materials Planner"],
                "urgency_cues": [],
                "positioning_statement": "Deckorators is the only brand in either deck that puts its actual brand tagline ('Designed to Endure. Built to Enjoy.') in the page <title> AND the hero headline — full alignment between owned-channel SEO, brand positioning, and visual hierarchy. The hero is restrained: a single lifestyle deck scene, white sans-serif overlay, single outline CTA. The four-column feature row below (Technology, Warranty, Durability, Color & Style) signals a structured, catalog-like buying journey.",
                "messaging_stance": {
                    "rational_vs_emotional": "mixed",
                    "value_vs_premium_signaling": "premium",
                    "lead_with": "brand_story",
                    "tone_one_word": "authoritative"
                },
                "design_critique": {
                    "hero_layout": "Wide outdoor lifestyle backdrop (wicker chairs, dining set, dark gray deck) with bold uppercase white sans-serif headline overlay. Eye lands on the headline first (by far the largest text in viewport), then the outline CTA, then the scene.",
                    "visual_consistency": "Strong. The bold uppercase sans-serif type system carries through from this hero into the paid Meta 'Stand Out™' campaign — owned and paid share a visual identity better than any other brand in the set.",
                    "cta_clarity": "Single outline pill ('Explore Our Products') — quieter than Trex's filled-chartreuse or Fiberon's red. The outline reads premium but reduces click pressure; the 'Shop Free Samples / Shipping Included' utility CTA is more urgent visually.",
                    "use_of_imagery": "Wide-angle lifestyle (people not visible but human presence implied by furniture). Below the fold shifts to product-detail and feature-callout treatment — a deliberate move from emotion to specification.",
                    "notable_design_choices": [
                        "Page <title> = brand tagline = hero headline — unusual SEO+positioning discipline",
                        "Surestone® Composite Decking technology callout right below the hero — proprietary tech named explicitly",
                        "Regional selector modal intercepts first-time visitors — a percentage of first impressions never see the actual hero",
                        "English / Français regional toggle — only US-focused brand offering localized French for Quebec"
                    ]
                },
                "products_pushed": ["Surestone® Composite Decking", "Voyage Decking", "Decking Collections", "Railing (Aluminum, Composite, Cable, Glass)", "Lighting"],
                "audience_signals": [
                    "Homeowners researching premium composite (lifestyle hero, free samples + shipping)",
                    "Contractors and builders (Pros nav, Find a Builder front-and-center)",
                    "Quebec/French-Canadian market (English/Français regional selector)"
                ],
                "what_this_homepage_does_well": [
                    "Tightest brand-system alignment in the set — page title, hero headline, paid Meta 'Stand Out™' lockup all share the same visual DNA",
                    "Free Samples + Shipping Included as a combined value prop — only brand in the set tying both into a single offer line",
                    "Quebec French regional selector is a real localization investment, not a token gesture",
                    "Surestone® tech name surfaced in the announcement bar — proprietary equity made visible upfront"
                ],
                "what_it_misses": [
                    "Regional selector modal blocks the hero on first-visit for many users — design-critique limitation that means our analysis represents a returning user's view, not first-time",
                    "The bold 'Stand Out / Word on the street' campaign equity in paid Meta isn't echoed verbatim in the hero — owned channel reverts to the corporate 'Designed to Endure' tagline rather than amplifying the bolder paid line",
                    "Four-column feature row below the hero is text-heavy — reads more like a catalog index than a journey; could be reframed as visual proof",
                    "No Spanish (and no English-only US fast-path — every visitor first hits a region selector)"
                ],
                "strategist_one_liner": "Deckorators runs the most disciplined owned-channel system in the set — brand tagline threaded from <title> to hero to Meta — but the bold 'Stand Out™' equity it's building in paid Meta is held back from the homepage in favor of a quieter corporate tagline.",
                "confidence": 0.86
            }
        },
    ],
}


def main(deployment: str) -> int:
    if deployment not in ANALYSES:
        print(f"unknown deployment: {deployment}", file=sys.stderr); return 2
    db = Path(os.environ.get("INTEL_DB_PATH", "data/intel.db"))
    print(f"deployment={deployment}  db={db}")
    conn = sqlite3.connect(db)
    try:
        # Clean prior auto-inserted rows for this deployment's brands
        for entry in ANALYSES[deployment]:
            conn.execute(
                "DELETE FROM homepage_promos WHERE competitor_id=? AND raw_json LIKE '%\"strategist_one_liner\"%'",
                (entry["competitor_id"],),
            )
        for entry in ANALYSES[deployment]:
            cid = entry["competitor_id"]
            a = entry["analysis"]
            conn.execute(
                """INSERT INTO homepage_promos (
                    competitor_id, source_id, observation_id, observed_at,
                    hero_hash, headline, subhead, primary_cta_text, primary_cta_url,
                    offer_claim, offer_value, offer_kind, expiration,
                    channel_callouts, urgency_cues, confidence, cache_hit, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cid, entry["source_id"], entry["observation_id"], entry["observed_at"],
                    None,  # no hero_hash for hand-authored entries
                    a["headline"], a["subhead"], a["primary_cta_text"], a["primary_cta_url"],
                    a["offer_claim"], a["offer_value"], a["offer_kind"], a["expiration"],
                    json.dumps(a["channel_callouts"]),
                    json.dumps(a["urgency_cues"]),
                    a["confidence"], 0,
                    json.dumps(a),
                ),
            )
            print(f"  inserted: {cid}  conf={a['confidence']:.2f}  one_liner={a['strategist_one_liner'][:80]}…")
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM homepage_promos").fetchone()[0]
        print(f"\nhomepage_promos rows in db: {n}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: persist_homepage_analyses.py <bobs|trex>", file=sys.stderr); sys.exit(2)
    sys.exit(main(sys.argv[1]))
