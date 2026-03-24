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
    -- Default (no desk filter): FULLY_PRIVATE ranks last (tier 2).
    -- When filtering to >= 8 desks, change tier to 0 to float private first,
    -- or apply the Retool version of this query for dynamic switching.
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
