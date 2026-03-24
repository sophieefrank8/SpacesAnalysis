-- =============================================================================
-- tandem.space — Space Ranking Query (Retool)
-- Paste into a Retool query component connected to your Neon resource.
-- All {{ }} blocks reference Retool component values.
--
-- Expected components:
--   minDesksFilter        — Number Input  (nullable)
--   maxDesksFilter        — Number Input  (nullable)
--   neighborhoodFilter    — Select        (nullable, string)
--   regionFilter          — Select        (nullable, string)
--   leaseTypeMultiselect  — Multiselect   (array, empty = no filter)
--   sharingKindFilter     — Select        (nullable, string)
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

    -- ── QUALITY (60%) ────────────────────────────────────────────────────────
    CASE
      WHEN fs."llmScore" IS NULL        THEN 0.5
      WHEN fs."llmScore" > 1            THEN LEAST(fs."llmScore" / 100.0, 1.0)
      ELSE GREATEST(LEAST(fs."llmScore", 1.0), 0.0)
    END AS quality_component,

    -- ── RECENCY (15%) ────────────────────────────────────────────────────────
    CASE
      WHEN fs."updatedAt" IS NULL  THEN 0.0
      ELSE (1.0 - LEAST(EXTRACT(EPOCH FROM (NOW() - fs."updatedAt")) / 86400.0
                   / ma.max_age_days, 1.0))
    END AS recency_component,

    -- ── DEMAND (25%) ─────────────────────────────────────────────────────────
    LEAST(fs.total_views / mv.max_total_views, 1.0) AS demand_component,

    -- ── PRIVACY SORT ─────────────────────────────────────────────────────────
    -- Tier 0: desk filter >= 8 AND space is FULLY_PRIVATE → float to top
    -- Tier 1: everything else → middle
    -- Tier 2: no desk filter active AND space is FULLY_PRIVATE → sink to bottom
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
