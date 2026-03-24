# Tandem Space Ranking System — Project Brief

This document is the complete context for the Tandem space ranking system. It covers the decisions made, the rationale behind them, the current state of all SQL, and what remains to be built. Use it as ground truth when continuing work on this system.

---

## What This Is

Tandem is a workspace marketplace. Spaces (office listings) are surfaced to Mates (tenants searching for office space) via a browse page. Previously, spaces were manually curated using an `is_top_space` flag. This system replaces that with an automated ranking score.

The ranking runs as a live-computed SQL query — no cache tables, no nightly jobs. The Neon database is read-only from the application layer, so all scoring happens at query time directly from source tables.

---

## The Formula

```
ranking_score = quality × 0.60 + recency × 0.15 + demand × 0.25
```

All components are normalised 0–1 before weighting. Final score is also 0–1.

### Pillar 1 — Quality (60%)

Source: `spaces.llmScore`

```sql
CASE
  WHEN "llmScore" IS NULL   THEN 0.5              -- neutral fallback
  WHEN "llmScore" > 1       THEN LEAST("llmScore" / 100.0, 1.0)  -- handle 0-100 scale
  ELSE GREATEST(LEAST("llmScore", 1.0), 0.0)      -- standard 0-1 scale
END
```

The LLM score is produced by a separate scoring pipeline that evaluates space photos and listing content. It is the strongest unbiased signal — requires no listing history.

### Pillar 2 — Recency (15%)

Source: `spaces.updatedAt`

```sql
-- Linear decay from most-recently-updated to oldest space in the result set
(1.0 - LEAST(days_since_update / max_age_days, 1.0))
```

- `max_age_days` is computed dynamically per query from the filtered result set, not a hardcoded constant
- The most recently updated listing scores 1.0; the oldest scores 0.0

### Pillar 3 — Demand (25%)

Source: `analytics_listing_viewed` (via `view_counts` CTE) and `space_tours_rolling`

```sql
LEAST(total_views / max_total_views, 1.0)
```

Views are normalised against the max in the filtered result set. `completed_tours_30d` is selected for display but not currently used in the ranking score — it's available for future use.

---

## Privacy Sort Tier

Separate from the ranking score — applied as a hard sort tier in `ORDER BY` before `ranking_score`.

| Condition | Tier | Behaviour |
|---|---|---|
| Desk filter ≥ 8 AND `FULLY_PRIVATE` | 0 | Floated to top |
| Everything else | 1 | Sorted by score |
| No desk filter AND `FULLY_PRIVATE` | 2 | Sunk to bottom |

Logic: when a Mate is searching for 8+ desks they likely want private space — surface it first. When browsing without a size filter they're probably exploring, not committed to private — don't let low-scoring private spaces crowd the top.

```sql
CASE
  WHEN {{ minDesksFilter.value >= 8 }}
    AND 'FULLY_PRIVATE'::space_sharing_enum = ANY("spaceSharing") THEN 0
  WHEN {{ minDesksFilter.value === null && maxDesksFilter.value === null }}
    AND 'FULLY_PRIVATE'::space_sharing_enum = ANY("spaceSharing") THEN 2
  ELSE 1
END AS privacy_sort
```

---

## Key Decisions & Rationale

### Why three pillars instead of nine?

The original design had nine signals. Correlation analysis on the SoHo dataset showed that views, matches, tours, match-to-tour ratio, and activity all shared r > 0.85 — they were all measuring the same thing ("this space is popular"). Stacking correlated signals makes the formula hard to reason about and impossible to tune sensibly. The three pillars were chosen because they are genuinely independent (max cross-pillar r = 0.34).

### Why views for demand instead of tours?

Views have broader coverage across the catalogue. Tours are the higher-intent signal but 68/81 SoHo spaces had zero tours, making it a poor differentiator at this stage. Views are used as the demand signal. `completed_tours_30d` is included in the output for visibility.

### Why is the Neon database read-only?

Neon is the production database. The application connects with a read-only role. This means no cache tables, no materialised views with writes, no nightly scoring jobs. The query runs live on every request.

### Why no A/B test?

The `is_top_space` manual curation is no longer being maintained, so there is no stable baseline to test against.

### Multicollinearity — what was dropped and why

| Signal | Reason dropped |
|---|---|
| Views + matches + tours + activity + match-to-tour | All r > 0.85 — kept views only |
| Building & neighbourhood quality (space_assessment) | Only 9% of spaces have assessments — too sparse |
| Conference room density | Correlated with activity cluster |
| Amenity richness | Correlated with popularity |
| Ops confirmation | Removed from recency; fully dropped — not enough differentiation to justify the join |

---

## Database Schema (relevant tables)

```
spaces
  id, title, status, llmScore, updatedAt, desksAvailable,
  minNumberOfDesks, maxNumberOfDesks, spaceSharing (array enum),
  sharingKind (enum), leaseType (enum), squareFootage,
  minPricePerDesk, maxPricePerDesk, minTotalPricePerMonth, maxTotalPricePerMonth,
  locationId → space_location.id

space_location
  id, address, neighborhood, region (space_location_region_enum), city

analytics_listing_viewed
  listing_id (= spaces.id), created_at, user_id

space_tours_rolling          -- pre-aggregated rolling window view
  space_id, tours_completed_30d
```

**Enum types (Postgres):**
- `space_sharing_enum`: includes `FULLY_PRIVATE`, `DEDICATED_SECTION`, `PRIVATE_ROOM`
- `space_location_region_enum`: e.g. `NYC`, `SF`, `BOS`
- `spaces_leasetype_enum`: e.g. `MONTHLY`, `ANNUAL`, `DAILY`

**Known gotcha:** Casting these enums in Retool requires explicit `::text` comparisons or `NULLIF(..., 'null')::enum` patterns to avoid errors when filter values are null/unset.

---

## Current SQL Files

### 1. Neon version (`space_ranking_neon.sql`)

Run directly in the Neon SQL editor. No Retool variables. Filters are commented out in the `WHERE` clause — uncomment as needed. Privacy sort defaults to tier 2 for `FULLY_PRIVATE` (private last) since there are no variables to detect filter state.

```sql
-- =============================================================================
-- tandem.space — Space Ranking Query (Neon / plain SQL)
-- Run directly in the Neon SQL editor. No Retool variables.
-- Filters can be added to the WHERE clause in filtered_spaces as needed.
--
-- Privacy sort (no active desk filter assumed):
--   FULLY_PRIVATE spaces rank last by default.
--   To float private spaces to the top instead, change privacy_sort:
--     CASE WHEN 'FULLY_PRIVATE' = ANY("spaceSharing") THEN 0 ELSE 1 END
-- =============================================================================

WITH view_counts AS (
  SELECT
    alv.listing_id AS space_id,
    COUNT(*)::float AS total_views
  FROM analytics_listing_viewed alv
  GROUP BY alv.listing_id
),

filtered_spaces AS (
  SELECT
    s.id,
    s.title,
    sl.address,
    sl.neighborhood,
    sl.region,
    s."desksAvailable",
    s."minNumberOfDesks",
    s."maxNumberOfDesks",
    s."spaceSharing",
    s."sharingKind",
    s."leaseType",
    s."squareFootage",
    s."llmScore",
    s."updatedAt",
    COALESCE(s."minTotalPricePerMonth", s."minPricePerDesk" * s."minNumberOfDesks") AS min_full_space_price,
    COALESCE(s."maxTotalPricePerMonth", s."maxPricePerDesk" * s."maxNumberOfDesks") AS max_full_space_price,
    COALESCE(vc.total_views, 0.0)          AS total_views,
    COALESCE(st.tours_completed_30d, 0)    AS completed_tours_30d
  FROM spaces s
  LEFT JOIN space_location sl          ON sl.id       = s."locationId"
  LEFT JOIN view_counts vc             ON vc.space_id = s.id
  LEFT JOIN space_tours_rolling st     ON st.space_id = s.id
  WHERE s.status = 'PUBLISHED'
  -- ── ADD FILTERS HERE ──────────────────────────────────────────────────────
  -- AND sl.neighborhood = 'NYC - Chelsea'
  -- AND s."desksAvailable" >= 8
  -- AND s."desksAvailable" <= 20
  -- AND sl.region = 'NYC'::space_location_region_enum
  -- AND s."leaseType"::text = ANY(ARRAY['MONTHLY','ANNUAL'])
  -- AND 'FULLY_PRIVATE'::space_sharing_enum = ANY(s."spaceSharing")
  -- ─────────────────────────────────────────────────────────────────────────
),

max_views AS (
  SELECT GREATEST(MAX(total_views), 1.0) AS max_total_views
  FROM filtered_spaces
),

max_age AS (
  SELECT GREATEST(EXTRACT(EPOCH FROM (NOW() - MIN("updatedAt"))) / 86400.0, 1.0) AS max_age_days
  FROM filtered_spaces
),

scored_spaces AS (
  SELECT
    fs.*,

    CASE
      WHEN fs."llmScore" IS NULL        THEN 0.5
      WHEN fs."llmScore" > 1            THEN LEAST(fs."llmScore" / 100.0, 1.0)
      ELSE GREATEST(LEAST(fs."llmScore", 1.0), 0.0)
    END AS quality_component,

    CASE
      WHEN fs."updatedAt" IS NULL  THEN 0.0
      ELSE (1.0 - LEAST(EXTRACT(EPOCH FROM (NOW() - fs."updatedAt")) / 86400.0
                   / ma.max_age_days, 1.0))
    END AS recency_component,

    LEAST(fs.total_views / mv.max_total_views, 1.0) AS demand_component,

    CASE
      WHEN 'FULLY_PRIVATE'::space_sharing_enum = ANY(fs."spaceSharing") THEN 2
      ELSE 1
    END AS privacy_sort

  FROM filtered_spaces fs
  CROSS JOIN max_views mv
  CROSS JOIN max_age ma
)

SELECT
  id                    AS space_id,
  title,
  address,
  neighborhood,
  region,
  "desksAvailable",
  "minNumberOfDesks",
  "maxNumberOfDesks",
  "spaceSharing",
  "sharingKind",
  "leaseType",
  "squareFootage",
  min_full_space_price,
  max_full_space_price,
  completed_tours_30d,
  total_views,
  ROUND(quality_component::numeric,  3) AS quality_component,
  ROUND(recency_component::numeric,  3) AS recency_component,
  ROUND(demand_component::numeric,   3) AS demand_component,
  ROUND((quality_component * 0.60 + recency_component * 0.15 + demand_component * 0.25)::numeric, 4) AS ranking_score
FROM scored_spaces
ORDER BY
  privacy_sort    ASC,
  ranking_score   DESC NULLS LAST,
  total_views     DESC NULLS LAST;
```

---

### 2. Retool version (`space_ranking_retool.sql`)

Paste into a Retool query component. All `{{ }}` blocks reference Retool component values. The privacy sort uses three tiers driven by live filter state.

**Expected Retool components:**

| Component | Type | Notes |
|---|---|---|
| `minDesksFilter` | Number Input | nullable |
| `maxDesksFilter` | Number Input | nullable |
| `neighborhoodFilter` | Select | nullable string |
| `regionFilter` | Select | nullable string |
| `leaseTypeMultiselect` | Multiselect | array, empty = no filter |
| `sharingKindFilter` | Select | nullable string |

```sql
-- =============================================================================
-- tandem.space — Space Ranking Query (Retool)
-- =============================================================================

WITH view_counts AS (
  SELECT
    alv.listing_id AS space_id,
    COUNT(*)::float AS total_views
  FROM analytics_listing_viewed alv
  GROUP BY alv.listing_id
),

filtered_spaces AS (
  SELECT
    s.id,
    s.title,
    sl.address,
    sl.neighborhood,
    sl.region,
    s."desksAvailable",
    s."minNumberOfDesks",
    s."maxNumberOfDesks",
    s."spaceSharing",
    s."sharingKind",
    s."leaseType",
    s."squareFootage",
    s."llmScore",
    s."updatedAt",
    COALESCE(s."minTotalPricePerMonth", s."minPricePerDesk" * s."minNumberOfDesks") AS min_full_space_price,
    COALESCE(s."maxTotalPricePerMonth", s."maxPricePerDesk" * s."maxNumberOfDesks") AS max_full_space_price,
    COALESCE(vc.total_views, 0.0)          AS total_views,
    COALESCE(st.tours_completed_30d, 0)    AS completed_tours_30d
  FROM spaces s
  LEFT JOIN space_location sl          ON sl.id       = s."locationId"
  LEFT JOIN view_counts vc             ON vc.space_id = s.id
  LEFT JOIN space_tours_rolling st     ON st.space_id = s.id
  WHERE s.status = 'PUBLISHED'
    AND ({{ minDesksFilter.value === null }} OR s."desksAvailable" >= {{ minDesksFilter.value }})
    AND ({{ maxDesksFilter.value === null }} OR s."desksAvailable" <= {{ maxDesksFilter.value }})
    AND (NULLIF('{{ neighborhoodFilter.value }}', 'null') IS NULL
         OR sl.neighborhood = NULLIF('{{ neighborhoodFilter.value }}', 'null'))
    AND (NULLIF('{{ regionFilter.value }}', 'null') IS NULL
         OR sl.region = NULLIF('{{ regionFilter.value }}', 'null')::space_location_region_enum)
    AND ({{ leaseTypeMultiselect.value.length === 0 }}
         OR s."leaseType"::text = ANY(ARRAY[{{ leaseTypeMultiselect.value.map(v => `'${v}'`).join(',') }}]::text[]))
    AND (NULLIF('{{ sharingKindFilter.value }}', 'null') IS NULL
         OR NULLIF('{{ sharingKindFilter.value }}', 'null')::space_sharing_enum = ANY(s."spaceSharing"))
),

max_views AS (
  SELECT GREATEST(MAX(total_views), 1.0) AS max_total_views
  FROM filtered_spaces
),

max_age AS (
  SELECT GREATEST(EXTRACT(EPOCH FROM (NOW() - MIN("updatedAt"))) / 86400.0, 1.0) AS max_age_days
  FROM filtered_spaces
),

scored_spaces AS (
  SELECT
    fs.*,

    CASE
      WHEN fs."llmScore" IS NULL        THEN 0.5
      WHEN fs."llmScore" > 1            THEN LEAST(fs."llmScore" / 100.0, 1.0)
      ELSE GREATEST(LEAST(fs."llmScore", 1.0), 0.0)
    END AS quality_component,

    CASE
      WHEN fs."updatedAt" IS NULL  THEN 0.0
      ELSE (1.0 - LEAST(EXTRACT(EPOCH FROM (NOW() - fs."updatedAt")) / 86400.0
                   / ma.max_age_days, 1.0))
    END AS recency_component,

    LEAST(fs.total_views / mv.max_total_views, 1.0) AS demand_component,

    CASE
      WHEN {{ minDesksFilter.value >= 8 }}
        AND 'FULLY_PRIVATE'::space_sharing_enum = ANY(fs."spaceSharing") THEN 0
      WHEN {{ minDesksFilter.value === null && maxDesksFilter.value === null }}
        AND 'FULLY_PRIVATE'::space_sharing_enum = ANY(fs."spaceSharing") THEN 2
      ELSE 1
    END AS privacy_sort

  FROM filtered_spaces fs
  CROSS JOIN max_views mv
  CROSS JOIN max_age ma
)

SELECT
  id                    AS space_id,
  title,
  address,
  neighborhood,
  region,
  "desksAvailable",
  "minNumberOfDesks",
  "maxNumberOfDesks",
  "spaceSharing",
  "sharingKind",
  "leaseType",
  "squareFootage",
  min_full_space_price,
  max_full_space_price,
  completed_tours_30d,
  total_views,
  quality_component,
  recency_component,
  demand_component,
  (quality_component * 0.60 + recency_component * 0.15 + demand_component * 0.25) AS ranking_score
FROM scored_spaces
ORDER BY
  privacy_sort    ASC,
  ranking_score   DESC NULLS LAST,
  total_views     DESC NULLS LAST;
```

---

## Retool Dashboard

A QA dashboard for the team to browse ranked spaces and validate the formula.

**Two queries:**

1. `getNeighbourhoods` — runs on load, populates neighbourhood dropdown:
```sql
SELECT DISTINCT sl.neighborhood
FROM spaces s
JOIN space_location sl ON sl.id = s."locationId"
WHERE s.status = 'PUBLISHED' AND sl.neighborhood IS NOT NULL
ORDER BY sl.neighborhood;
```

2. `getRankedSpaces` — the Retool SQL above, triggered by a Search button.

**Layout:**
- Filter bar: neighbourhood Select, desk count Number Input, Search button
- Hint text: `{{ deskCountInput.value >= 8 ? "🔒 Private spaces ranked first" : "" }}`
- Results Table bound to `{{ getRankedSpaces.data }}`
- Sidebar panel showing per-pillar breakdown for selected row

**Column visibility in table:** show `address`, `privacy_type`, `ranking_score`, `quality_component`, `recency_component`, `demand_component`, `days_since_update`, `completed_tours_30d`, `total_views`. Hide `id`, raw enum fields.

---

## Open Questions / Phase 2 Candidates

- **walk_score** — present in `spaces` table but 99–100 for all dense urban spaces, no differentiation. Useful for suburban markets. Phase 2.
- **Conference room density** — available (`numberOfConferenceRooms / squareFootage`) but correlated with activity. Phase 2 if A/B data shows Mates care about meeting room density specifically.
- **Building & neighbourhood quality** — `space_assessment` table has qualitative summaries and confidence ratings across 5 dimensions (transit, restaurants, safety, building amenities, architecture). Only ~9% of spaces have assessments currently. Phase 2 once coverage improves past ~50%.
- **Normalisation scope** — current query normalises views and recency against the filtered result set. This means scores are relative to what's shown, not absolute. Consider whether global normalisation (all published spaces) is preferable when integrating into the main browse sort.
- **tours_completed_30d** — included in SELECT output but not in the ranking formula. Consider weighting it into demand alongside or instead of views once rolling window coverage is validated.
- **Price and neighborhood mixing** — when no price filter is active, use `NTILE(3)` to dynamically split results into three equal price tiers and interleave them, also mixing by neighborhood to prevent geographic clustering. Phase 2.

---

## What's Next

1. **Validate in Retool** — share the dashboard with the team, collect feedback on ranking quality across multiple neighbourhoods and desk sizes.
2. **Integrate into browse** — replace `is_top_space` ordering in the main browse query with `ranking_score` + `privacy_sort`.
3. **Coverage QA** — periodically run a neighbourhood × size bucket coverage check (≥3 spaces with score > 0.6 per bucket). Flag thin markets to ops.
4. **Formula tuning** — after 2–3 weeks of team use, revisit pillar weights based on qualitative feedback and any observable changes in tour or match rate.
