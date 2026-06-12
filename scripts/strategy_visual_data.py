#!/usr/bin/env python
"""Strategy-doc visual data + reference-board curation.

For each deployment, define:
  - the strategic themes for the reference board
  - which specific ads belong under each theme
  - a strategic caption (written here, NOT pulled from the ad's adDescription)
  - a positioning quadrant (axis labels + per-brand x/y coordinates 0-1)

Per the brand-marketing-mode methodology: captions should explain WHY each
reference is here strategically, not summarize what the ad already says.

Charts pull live aggregate data from the deployment's db; reference-board
entries and quadrant positions are curated by ad_archive_id / brand_id.
"""
from __future__ import annotations

DEPLOYMENTS = {
    "bobs": {
        "db_path": "data/intel.db",
        "subject_brand": "bobs",
        "brand_labels": {
            "bobs": "Bob's",
            "ashley": "Ashley",
            "rooms_to_go": "Rooms To Go",
            "raymour_flanigan": "Raymour & Flanigan",
            "value_city": "Value City",
            "american_freight": "American Freight",
            "wayfair": "Wayfair",
            "revlon": "Revlon (control)",
        },
        "brand_order": ["bobs", "raymour_flanigan", "wayfair", "ashley", "american_freight",
                         "rooms_to_go", "value_city", "revlon"],
        "header_eyebrow": "Horizon Commerce · Brand Marketing Mode",
        "header_meta": "Strategy Brief · Confidential",
        "hero": {
            "lead_in": "The opportunity is",
            "italic": "warmth in a coasting category",
            "subtitle": "A competitive read for Bob's Discount Furniture. Seven brands in market, six of them coasting on evergreen tone. The category has an emotional vacancy and a structural gap. Bob's sits in the only position that can credibly take both.",
        },
        # Positioning quadrant — axis labels + x/y (each 0..1) + bubble-size sourcing
        "quadrant": {
            "title": "Where each brand sits in the category",
            "x_axis": ("Value tier", "Premium positioning"),  # left, right
            "y_axis": ("Store-driven", "Digital-native"),     # bottom, top
            "brands": {
                # x = price/positioning (0=deep value, 1=premium)
                # y = channel strategy (0=brick-and-mortar driven, 1=fully digital-native)
                "bobs":              (0.42, 0.55),
                "ashley":            (0.30, 0.22),
                "raymour_flanigan":  (0.62, 0.28),
                "wayfair":           (0.45, 0.92),
                "american_freight":  (0.12, 0.20),
                "value_city":        (0.22, 0.18),
                "rooms_to_go":       (0.38, 0.18),
            },
        },
        "quadrant_caption": "Bob's is the only brand bridging store-driven retail and digital-native marketing. The mid-position is contested by no one.",
        "reference_themes": [
            {
                "title": "The Southeast expansion story Bob's is leaving on the table",
                "intro": "Four simultaneous new-store opening flights — currently awareness-only. The narrative thread connecting them isn't being told.",
                "items": [
                    {"ad_archive_id": "1670780951062396", "caption": "Spartanburg now-open ad. Reads as a transactional announcement; could anchor a regional-growth story instead."},
                    {"ad_archive_id": "2053917525539916", "caption": "University City opening. Same template — efficient to produce, narratively flat."},
                    {"ad_archive_id": "1474046854740071", "caption": "Pineville opening June 8. The specific date is the most urgent signal in any Bob's ad this window."},
                ],
            },
            {
                "title": "The price-anchored wedge Bob's already owns",
                "intro": "These are the workhorse Meta units running underneath the brand. They name the value wedge clearly — $499 sectionals, $599 dining sets. Cleanly produced, but emotionally restrained.",
                "items": [
                    {"ad_archive_id": "1342504074525431", "caption": "$499 sectionals — the clearest price wedge in Bob's paid Meta. Studio product, rational hook."},
                    {"ad_archive_id": "1002956615494711", "caption": "$599 dining set. Same template. Could carry an emotional cut alongside the rational one."},
                ],
            },
            {
                "title": "What the rest of the set is doing instead",
                "intro": "Three different versions of the same problem — evergreen tone, no urgency, brand-equity claims standing in for product news.",
                "items": [
                    {"ad_archive_id": "1208824624354029", "comp_override": "wayfair", "caption": "Wayfair Verified — the strongest digital-native trust play in the set, but a vague claim without proof."},
                    {"ad_archive_id": "1521489726006525", "comp_override": "raymour_flanigan", "caption": "Raymour leans on 'durable, built for years' — quality framing without specifics."},
                    {"ad_archive_id": "1004972765383057", "comp_override": "raymour_flanigan", "caption": "Raymour's 'everyone's favorite' — best-seller social proof, the most-repeated frame in the set."},
                ],
            },
        ],
        # Strings to highlight inline. Brand names auto-highlight from brand_labels.
        "highlight_phrases": [
            "Top-rated AND affordable",
            "the most distinctive asset in your competitive set",
            "the warm modern American furniture brand",
            "Bob himself",
            "53% are lifestyle",
            "Wayfair-ish",
            "Apartment Therapy",
            "House Beautiful",
            "Consumer Reports",
            "Pineville",
            "Spartanburg",
            "University City",
            "Greenville",
            "$799 Treasured queen mattress",
            "$499 sectionals",
            "$599 dining sets",
            "Memorial Day",
            "60 Months",
            "@wayfaircreators",
            "#WayfairElevate",
            "80 years",
            "3-day delivery",
            "Mattress Mack",
            "Wendy's Wendy",
        ],
    },
    "trex": {
        "db_path": "data/trex.db",
        "subject_brand": "trex",
        "brand_labels": {
            "trex": "Trex",
            "timbertech": "TimberTech",
            "fiberon": "Fiberon",
            "deckorators": "Deckorators",
        },
        "brand_order": ["trex", "timbertech", "deckorators", "fiberon"],
        "header_eyebrow": "Horizon Commerce · Brand Marketing Mode",
        "header_meta": "Strategy Brief · Confidential",
        "hero": {
            "lead_in": "The opportunity is",
            "italic": "emotion at category-leader scale",
            "subtitle": "A competitive read for Trex. Three active brands in market, one going dark. The category leader is doing the most things; the #2 is doing the most distinctive thing. The emotional ground is being claimed — and Trex has the assets to defend it.",
        },
        "quadrant": {
            "title": "Where each brand sits in the category",
            "x_axis": ("Rational / spec-led", "Emotional / aspirational"),
            "y_axis": ("Dealer-routed", "DTC mass-reach"),
            "brands": {
                # x = rational vs emotional appeal (0=rational, 1=emotional)
                # y = dealer-routed vs DTC (0=dealer, 1=full DTC)
                "trex":         (0.30, 0.92),
                "timbertech":   (0.72, 0.62),
                "deckorators":  (0.70, 0.35),
                "fiberon":      (0.50, 0.12),
            },
        },
        "quadrant_caption": "Trex owns DTC scale alone. TimberTech is taking the emotional ground. Deckorators is building a disciplined distinctive-asset play. Fiberon is dark in the window — a competitive vacancy.",
        "reference_themes": [
            {
                "title": "Trex's category-expansion plays",
                "intro": "Two product launches in one week — both opening adjacent buyer pools that didn't previously consider composite decking. The creative is engineering-led; the opportunity is emotional.",
                "items": [
                    {"ad_archive_id": "1010053428128216", "caption": "Trex Refuge fire-resistant launch. Product is the proof, but the protagonist (a wildfire-zone family) is missing."},
                    {"ad_archive_id": "1328248300131707", "caption": "'Where fire resistance and West Coast–inspired design come together.' The aspirational lane Trex could lean into harder."},
                    {"ad_archive_id": "1284398363250516", "caption": "Marine-grade dock claim: '2 million sq. ft. of waterfront.' Enormous proof point sitting inside static creative."},
                    {"ad_archive_id": "1275384814734769", "caption": "Dock-specific product spec ad. The use case is uncontested in the set — no peer is targeting marine."},
                ],
            },
            {
                "title": "Deckorators's distinctive-asset discipline",
                "intro": "A single brand idea — Stand Out™ / 'the neighbors will notice' — expressed with consistent typography, bold layouts, and US/Quebec localization. The most disciplined creative system in the set.",
                "items": [
                    {"ad_archive_id": "2040803979833111", "caption": "The flagship Stand Out™ lockup. Distinctive type treatment, consistent across the campaign."},
                    {"ad_archive_id": "955875424004471", "caption": "Quebec French localization. Same campaign, different market — disciplined segment-aware deployment."},
                    {"ad_archive_id": "1683394976131723", "caption": "Quebec variant of 'Word on the street' — localized line, same brand asset."},
                ],
            },
            {
                "title": "TimberTech's lifestyle-first brand-build",
                "intro": "86% lifestyle, 79% aesthetic hook, 68% emotional appeal. TimberTech is taking emotional ground in a category where the rest of the set is in spec mode.",
                "items": [
                    {"ad_archive_id": "1187361900089634", "caption": "Deck Design Guide lead magnet. Lifestyle-led, low-friction entry into the long buying journey."},
                    {"ad_archive_id": "1590401239756102", "caption": "Same campaign in a different aspirational frame. TimberTech treats the design phase as the seduction layer."},
                ],
            },
            {
                "title": "Trex's existing funnel infrastructure — ripe to expand",
                "intro": "Sample funnel, cost calculator, TrexPro contractor finder. The pieces are built and converting; the opportunity is to wrap them in emotional creative and localized variants.",
                "items": [
                    {"ad_archive_id": "1016884224184199", "caption": "Sample-funnel hero: 'Feel it, see it, test it.' Existing creative that could carry an offer overlay or Spanish-language cut."},
                    {"ad_archive_id": "1231724215059714", "caption": "Cost calculator funnel. The narrative version ('what 400 sq ft actually costs') would outperform the tool itself."},
                ],
            },
        ],
        "highlight_phrases": [
            "200 ads",
            "115 ads",
            "41 ads",
            "0 ads",
            "Refuge",
            "fire-resistant",
            "submersible/marine-grade",
            "dock owners",
            "wildfire-zone homeowners",
            "Stand Out",
            "Quebec",
            "French-Canadian",
            "free samples",
            "TrexPro",
            "contractor-testimonial engine",
            "category-leading distinctive brand asset",
            "category-expansion moves",
            "86% lifestyle",
            "53% lifestyle",
            "68% emotional",
            "30% emotional",
            "61% rational",
            "Ehrenberg-Bass",
            "Byron Sharp",
            "Binet & Field",
            "Kuiken Bros",
            "Ganahl Lumber",
            "Abbe Lumber",
            "Buildpro",
            "TIU-5T8",
            "the brand that's expanding what composite decking can be",
        ],
    },
}
