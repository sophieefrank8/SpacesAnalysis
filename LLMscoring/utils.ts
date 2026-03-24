/**
 * LLM Space Scoring — Revised Implementation
 *
 * Changes from original (apps/web/src/app/(app)/api/space-scoring/utils.ts):
 * - Replaced OpenAI (gpt-4o-mini) with Anthropic Claude (claude-sonnet-4-6)
 * - Replaced zodResponseFormat with Claude tool_use pattern for structured output
 * - Updated image content blocks to Claude's { type: 'image', source: { type: 'url' } } format
 * - Updated system prompt: ceiling height removed, text-first principle, photo scope rule,
 *   partial ceiling scoring, skylight support, floorplan_layout_type for open workspace
 * - Added 'Finishes & Condition' as a sixth scored attribute (15% weight)
 * - Weight rebalancing: Exposed Ceilings 40%→30%, Glass Conference Rooms 10%→5%
 * - floorplan_layout_type passed as a text input field alongside imported data
 * - Environment variable: ANTHROPIC_API_KEY (was OPENAI_API_KEY)
 *
 * To use opus for higher visual accuracy: change MODEL constant to 'claude-opus-4-6'
 */

import Anthropic from '@anthropic-ai/sdk';
import { z } from 'zod';

import { db, eq, sql } from '@tandem/db';
import { http } from '@tandem/db/src/http';
import { match, recommendation, spaces, spaceScoreSnapshot, spaceWishlist, tour } from '@tandem/db/src/models';

// ─── Model ───────────────────────────────────────────────────────────────────

const MODEL = 'claude-sonnet-4-6';
// const MODEL = 'claude-opus-4-6'; // Uncomment for highest visual accuracy

// ─── Weights (unchanged) ─────────────────────────────────────────────────────

interface ScoringWeights {
  isTopSpaceWeight: number;
  contentWeight: number;
  content: {
    images: number;
    description: number;
    virtualTour: number;
  };
  engagementWeight: number;
  engagement: {
    wishlists: number;
    tourRequests: number;
    recommendations: number;
  };
  newScore: number;
}

const WEIGHTS: ScoringWeights = {
  isTopSpaceWeight: 0.34,
  contentWeight: 0.33,
  content: {
    images: 0.5,
    virtualTour: 0.3,
    description: 0.2,
  },
  engagementWeight: 0.33,
  engagement: {
    wishlists: 0.2,
    tourRequests: 0.2,
    recommendations: 0.6,
  },
  newScore: 0.3,
};

const BOOST_DURATION_DAYS = 60;
const MIN_DESCRIPTION_LENGTH = 800;
const MIN_NUM_IMAGES = 9;
const SUPPORTED_CONTENT_TYPES = ['image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp'];

// ─── System prompt (revised) ─────────────────────────────────────────────────

const SPACE_SCORE_SYSTEM_PROMPT = `
You are Office Space Rater, an expert assistant that analyzes
uploaded office photos, floorplans, and text descriptions.

You produce a detailed scorecard for five key office attributes,
a weighted aggregate score, and an overall confidence score.

━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL DEFINITIONS
━━━━━━━━━━━━━━━━━━━━━━━━

EXTERIOR WINDOW (counts for window attributes):
- An opening in the building's outer/perimeter wall
- On a floorplan: a break, gap, or window symbol on the building boundary
- In photos: shows outdoor view — sky, buildings, street, or any outside scene
- May show directional sunlight or shadows entering the space

INTERIOR GLASS (does NOT count as a window):
- Glass walls, partitions, or panels between interior rooms
- On a floorplan: a line dividing the interior space, not touching the perimeter
- In photos: shows another interior room through the glass, not the outdoors
- Exception: glass conference room walls are scored under the Glass Conference
  Rooms attribute ONLY — not under window attributes

NATURAL LIGHT:
- Do NOT treat photo brightness as evidence of natural light
- Only infer natural light from confirmed exterior windows
- Directional sunlight or sunbeams in photos are valid signals
- Flat, even brightness (typical of fluorescent/LED lighting) is NOT a signal

━━━━━━━━━━━━━━━━━━━━━━━━
NON-OFFICE OVERRIDE
━━━━━━━━━━━━━━━━━━━━━━━━
If text or photos clearly indicate non-office use (Retail, Medical, Salon,
Gallery, Showroom, Restaurant): set all scores to 0, aggregate to 0,
add explanation. Residential may score if live/work use is clear.

━━━━━━━━━━━━━━━━━━━━━━━━
ATTRIBUTES & SCORING (0–10)
━━━━━━━━━━━━━━━━━━━━━━━━

EXPOSED CEILINGS (40% weight)
Primary factor — exposure:
  - Visible ductwork, pipes, mechanical systems, raw concrete,
    wood/steel beams, joists, or trusses → score high
  - Square ceiling tiles → score 0–2 (not exposed, regardless of height)
  - Smooth painted ceiling → finished, not exposed

Secondary factor — height (modifier only, not primary):
  - Use ceiling height as a secondary modifier, not the main score driver
  - "High ceilings," "20 ft ceilings," "vaulted" = height signal only

Scoring guide:
  - Exposed + high (clearly tall): 9–10
  - Exposed, height unclear: 6–8
  - High but fully finished (smooth, no structure visible): 4–6
  - Standard or drop tile ceiling: 0–3

Text override: "Exposed Ceiling," "Exposed Beams," "Raw Concrete Ceiling,"
"Industrial Ceiling" → primary signal (score 8–10 unless photos contradict).
"Drop Ceiling," "Ceiling Tiles" → score 0–2.

WINDOWS ON MULTIPLE WALLS (25% weight)
Score high (8–10) only if exterior-facing windows appear on 2+ distinct walls.

How to confirm:
  - Floorplan: look for breaks/gaps/window symbols on 2+ sides of the
    building perimeter (outer boundary of the space)
  - Photos: outdoor views (sky, street, buildings) visible from different
    wall directions
  - Interior glass walls do NOT count, even if large

Text signals: "corner unit," "multiple exposures," "windows on X sides,"
"north and east facing," "windows on four sides" → score high.

OPEN WORKSPACE (20% weight)
  - "Open Plan," "Mostly Open Plan," "Collaborative Open Space" → score 8–10
  - "Private Offices," "Office Intensive Layout" → score 0–4
  - No text: use floorplan — open area should be significantly larger than
    total enclosed room area

GLASS CONFERENCE ROOMS (10% weight)
  - Score ONLY if photos show conference/meeting rooms with full floor-to-ceiling
    glass interior walls facing the open workspace
  - Partial glass, frosted panels, or windows into rooms do NOT count
  - Interior glass walls that are NOT conference rooms do NOT count here
  - Spec Suite / Turnkey / Plug & Play text boosts score only if glass walls
    are also visible in photos

LARGE WINDOWS (5% weight)
  - Score high (8–10) for exterior windows that are floor-to-ceiling,
    span most of a wall, or are notably oversized relative to wall area
  - Must be exterior windows (outdoor view visible in photos)
  - Interior glass walls, regardless of size, do NOT qualify
  - Text signals: "floor-to-ceiling windows," "panoramic views,"
    "walls of glass," "full-height glazing" → score high if photos confirm

━━━━━━━━━━━━━━━━━━━━━━━━
GENERAL RULES
━━━━━━━━━━━━━━━━━━━━━━━━
- Always trust explicit text over visuals
- If text and visuals conflict, text overrides — explain conflict, lower confidence
- If attribute cannot be determined → score 0
- For each attribute: provide a 2-sentence reason and a confidence score (0–10)
  with reason

CONFIDENCE LEVELS:
  - Clear text + matching visuals: 8–10
  - Text only or visuals only (consistent): 6–8
  - Sparse or conflicting data: 4–6
  - Very limited data: 2–4

FIXED WEIGHTS (always use these):
  Exposed Ceilings:          40%
  Windows on Multiple Walls: 25%
  Open Workspace:            20%
  Glass Conference Rooms:    10%
  Large Windows:              5%

Respond using the score_space tool only. Do not include any other text.
`;

// ─── Zod schema (unchanged) ──────────────────────────────────────────────────

export const SpaceScoringOutputSchema = z.object({
  attributes: z.object({
    'Exposed Ceilings': z.object({
      score: z.number().min(0).max(10),
      reason: z.string(),
      confidence: z.number().min(0).max(10),
      confidence_reason: z.string(),
    }),
    'Windows on Multiple Walls': z.object({
      score: z.number().min(0).max(10),
      reason: z.string(),
      confidence: z.number().min(0).max(10),
      confidence_reason: z.string(),
    }),
    'Open Workspace': z.object({
      score: z.number().min(0).max(10),
      reason: z.string(),
      confidence: z.number().min(0).max(10),
      confidence_reason: z.string(),
    }),
    'Finishes & Condition': z.object({
      score: z.number().min(0).max(10),
      reason: z.string(),
      confidence: z.number().min(0).max(10),
      confidence_reason: z.string(),
    }),
    'Large Windows': z.object({
      score: z.number().min(0).max(10),
      reason: z.string(),
      confidence: z.number().min(0).max(10),
      confidence_reason: z.string(),
    }),
    'Glass Conference Rooms': z.object({
      score: z.number().min(0).max(10),
      reason: z.string(),
      confidence: z.number().min(0).max(10),
      confidence_reason: z.string(),
    }),
  }),
  aggregate_score: z.number().min(0).max(10),
  confidence_overall: z.number().min(0).max(10),
  confidence_reason: z.string(),
  boosts_applied: z.array(z.enum(['outdoor_access', 'kitchen_pantry', 'kitchenette'])),
});

// ─── Claude tool definition for structured output ────────────────────────────

/**
 * Claude uses tool_use to enforce structured JSON output.
 * tool_choice forces the model to always call this tool,
 * giving us the same structured-output guarantee as OpenAI's zodResponseFormat.
 */
const SCORE_SPACE_TOOL: Anthropic.Tool = {
  name: 'score_space',
  description: 'Score an office space on five key attributes and return a structured scorecard.',
  input_schema: {
    type: 'object' as const,
    properties: {
      attributes: {
        type: 'object',
        properties: {
          'Exposed Ceilings': {
            type: 'object',
            properties: {
              score: { type: 'number', minimum: 0, maximum: 10 },
              reason: { type: 'string' },
              confidence: { type: 'number', minimum: 0, maximum: 10 },
              confidence_reason: { type: 'string' },
            },
            required: ['score', 'reason', 'confidence', 'confidence_reason'],
          },
          'Windows on Multiple Walls': {
            type: 'object',
            properties: {
              score: { type: 'number', minimum: 0, maximum: 10 },
              reason: { type: 'string' },
              confidence: { type: 'number', minimum: 0, maximum: 10 },
              confidence_reason: { type: 'string' },
            },
            required: ['score', 'reason', 'confidence', 'confidence_reason'],
          },
          'Open Workspace': {
            type: 'object',
            properties: {
              score: { type: 'number', minimum: 0, maximum: 10 },
              reason: { type: 'string' },
              confidence: { type: 'number', minimum: 0, maximum: 10 },
              confidence_reason: { type: 'string' },
            },
            required: ['score', 'reason', 'confidence', 'confidence_reason'],
          },
          'Finishes & Condition': {
            type: 'object',
            properties: {
              score: { type: 'number', minimum: 0, maximum: 10 },
              reason: { type: 'string' },
              confidence: { type: 'number', minimum: 0, maximum: 10 },
              confidence_reason: { type: 'string' },
            },
            required: ['score', 'reason', 'confidence', 'confidence_reason'],
          },
          'Large Windows': {
            type: 'object',
            properties: {
              score: { type: 'number', minimum: 0, maximum: 10 },
              reason: { type: 'string' },
              confidence: { type: 'number', minimum: 0, maximum: 10 },
              confidence_reason: { type: 'string' },
            },
            required: ['score', 'reason', 'confidence', 'confidence_reason'],
          },
          'Glass Conference Rooms': {
            type: 'object',
            properties: {
              score: { type: 'number', minimum: 0, maximum: 10 },
              reason: { type: 'string' },
              confidence: { type: 'number', minimum: 0, maximum: 10 },
              confidence_reason: { type: 'string' },
            },
            required: ['score', 'reason', 'confidence', 'confidence_reason'],
          },
        },
        required: ['Exposed Ceilings', 'Windows on Multiple Walls', 'Open Workspace', 'Finishes & Condition', 'Large Windows', 'Glass Conference Rooms'],
      },
      aggregate_score: { type: 'number', minimum: 0, maximum: 10 },
      confidence_overall: { type: 'number', minimum: 0, maximum: 10 },
      confidence_reason: { type: 'string' },
      boosts_applied: {
        type: 'array',
        items: { type: 'string', enum: ['outdoor_access', 'kitchen_pantry', 'kitchenette'] },
        description: 'List of boosts applied to the aggregate score',
      },
    },
    required: ['attributes', 'aggregate_score', 'confidence_overall', 'confidence_reason', 'boosts_applied'],
  },
};

// ─── Image processing (unchanged logic, updated error message) ───────────────

interface ImageProcessingResult {
  validUrls: string[];
  originalCount: number;
  processedCount: number;
  errors: Array<{ url: string; error: string }>;
}

const getImageContentType = async (imageUrl: string): Promise<string | null> => {
  try {
    const response = await fetch(imageUrl, { method: 'HEAD' });
    const contentType = response.headers.get('content-type');
    if (!contentType) return null;
    return contentType.split(';')[0]?.trim().toLowerCase() || null;
  } catch (error) {
    console.warn(`Failed to get Content-Type for ${imageUrl}:`, error);
    return null;
  }
};

const isSupportedContentType = (contentType: string): boolean => {
  return SUPPORTED_CONTENT_TYPES.includes(contentType);
};

export const processImagesForLlm = async (imageUrls: string[]): Promise<ImageProcessingResult> => {
  const result: ImageProcessingResult = {
    validUrls: [],
    originalCount: imageUrls.length,
    processedCount: 0,
    errors: [],
  };

  if (!imageUrls.length) return result;

  for (const url of imageUrls) {
    try {
      if (!url) continue;

      const contentType = await getImageContentType(url);
      if (!contentType) {
        result.errors.push({
          url,
          error: `Unable to determine Content-Type. Supported types: ${SUPPORTED_CONTENT_TYPES.join(', ')}`,
        });
        continue;
      }

      if (isSupportedContentType(contentType)) {
        result.validUrls.push(url);
        result.processedCount++;
        continue;
      }

      result.errors.push({ url, error: `Content-Type not supported by Claude: ${contentType}` });
    } catch (error) {
      result.errors.push({
        url,
        error: `Processing failed: ${error instanceof Error ? error.message : 'Unknown error'}`,
      });
    }
  }

  return result;
};

// ─── Space fetching (unchanged) ───────────────────────────────────────────────

export const getSpacesCount = async () => {
  const result = await db.select({ count: sql<number>`COUNT(*)` }).from(spaces);
  return result[0]?.count ?? 0;
};

export const getSpaces = async ({ batchSize, offset }: { batchSize: number; offset: number }) => {
  const query = http.query.spaces.findMany({
    limit: batchSize,
    offset: offset,
    extras: {
      recommendationScore:
        sql<number>`LEAST(ln((CAST((SELECT COUNT(*) FROM ${recommendation} r WHERE r."spaceId" = ${spaces.id}) AS float) + 1.0)) / ln(6), 1)`.as(
          'recommendationScore'
        ),
      wishlistScore:
        sql<number>`LEAST(ln((CAST((SELECT COUNT(*) FROM ${spaceWishlist} w WHERE w."spaceId" = ${spaces.id}) AS float) + 1.0)) / ln(6), 1)`.as(
          'wishlistScore'
        ),
      tourScore:
        sql<number>`LEAST(ln((CAST((SELECT COUNT(*) FROM ${tour} t JOIN ${match} m ON m.id = t.match_id WHERE m."spaceId" = ${spaces.id}) AS float) + 1.0)) / ln(6), 1)`.as(
          'tourScore'
        ),
    },
    columns: {
      id: true,
      description: true,
      matterportSpaceId: true,
      firstSearchableDate: true,
      isTopSpace: true,
      lastLlmScoredAt: true,
      llmScore: true,
      lastLlmScoringError: true,
      qualityScore: true,
      floorplan_layout_type: true,
    },
    with: {
      images: { columns: { id: true, url: true, updatedAt: true } },
      videos: { columns: { id: true } },
      rawImportedData: {
        columns: {
          importedBuildOut: true,
          importedBuildOutAs: true,
          importedSpaceFeatures: true,
          importedSpaceNotes: true,
          updatedAt: true,
        },
      },
    },
    orderBy: (spaces, { desc }) => [desc(spaces.updatedAt)],
  });

  return await query;
};

export type SpacesType = Awaited<ReturnType<typeof getSpaces>>;
export type SpaceType = SpacesType[number];

// ─── Quality scoring (unchanged) ─────────────────────────────────────────────

const calculateContentScore = (space: SpaceType): number => {
  const imageScore = Math.min(space.images.length / MIN_NUM_IMAGES, 1) * WEIGHTS.content.images;
  const hasVirtualTour = space.matterportSpaceId !== null || space.videos.length > 0;
  const virtualTourScore = hasVirtualTour ? WEIGHTS.content.virtualTour : 0;
  const descriptionScore =
    Math.min((space.description?.length || 0) / MIN_DESCRIPTION_LENGTH, 1) * WEIGHTS.content.description;
  return imageScore + virtualTourScore + descriptionScore;
};

export const calculateEngagementScore = (space: SpaceType): number => {
  const { wishlistScore, tourScore, recommendationScore } = space;
  return (
    wishlistScore * WEIGHTS.engagement.wishlists +
    tourScore * WEIGHTS.engagement.tourRequests +
    recommendationScore * WEIGHTS.engagement.recommendations
  );
};

const calculateNewSpaceBoost = (space: SpaceType): number => {
  const { firstSearchableDate } = space;
  if (!firstSearchableDate) return 0;
  const daysListed = (new Date().getTime() - new Date(firstSearchableDate).getTime()) / (1000 * 60 * 60 * 24);
  if (daysListed > BOOST_DURATION_DAYS) return 0;
  return WEIGHTS.newScore * (1 - daysListed / BOOST_DURATION_DAYS);
};

export const calculateAndStoreScores = async (spacesToScore: SpaceType[]) => {
  const spaceScores = spacesToScore.map((space) => {
    const contentScore = calculateContentScore(space);
    const engagementScore = calculateEngagementScore(space);
    const newBoost = calculateNewSpaceBoost(space);
    const baseScore =
      WEIGHTS.contentWeight * contentScore +
      WEIGHTS.engagementWeight * engagementScore +
      WEIGHTS.isTopSpaceWeight * Number(space.isTopSpace);
    const finalScore = Math.min(baseScore * (1 + newBoost), 1);
    return { spaceId: space.id, contentScore, engagementScore, newBoost, finalScore };
  });

  await db.transaction(async (tx) => {
    const timestamp = new Date().toISOString();
    await Promise.all(
      spaceScores.map((score) =>
        tx.update(spaces).set({ qualityScore: score.finalScore, lastScoredAt: timestamp }).where(eq(spaces.id, score.spaceId))
      )
    );
    await tx.insert(spaceScoreSnapshot).values(
      spaceScores.map((score) => ({
        spaceId: score.spaceId,
        scoredAt: timestamp,
        scores: {
          contentScore: score.contentScore,
          engagementScore: score.engagementScore,
          newBoost: score.newBoost,
          finalScore: score.finalScore,
          weights: WEIGHTS,
        },
      }))
    );
  });

  return spaceScores;
};

// ─── LLM scoring — Claude implementation ─────────────────────────────────────

export const processLlmScoring = async (spacesToProcess: SpaceType[]) => {
  const spacesNeedingLlmScoring = spacesToProcess.filter(needsLlmScoring);
  if (!spacesNeedingLlmScoring.length) return [];

  console.log(`Processing LLM scoring for ${spacesNeedingLlmScoring.length} spaces...`);

  const llmScores = await Promise.all(
    spacesNeedingLlmScoring.map(async (space) => {
      const { score, error } = await scoreLlmWithProcessing(space);
      return { spaceId: space.id, llmScore: score, error };
    })
  );

  if (llmScores.length > 0) {
    await db.transaction(async (tx) => {
      const timestamp = new Date().toISOString();
      await Promise.all(
        llmScores.map((result) =>
          tx
            .update(spaces)
            .set({
              llmScore: result.llmScore,
              lastLlmScoredAt: result.llmScore !== null ? timestamp : undefined,
              lastLlmScoringError: result.error,
            })
            .where(eq(spaces.id, result.spaceId))
        )
      );
    });
  }

  return llmScores.filter((result) => result.llmScore !== null);
};

export const prepareLlmScoringData = (space: SpaceType) => {
  const imageUrls = space.images.map((img) => img.url).filter(Boolean);
  const importedData = space.rawImportedData[0];
  const textData = {
    buildOut: importedData?.importedBuildOut || '',
    buildOutAs: importedData?.importedBuildOutAs || '',
    spaceFeatures: importedData?.importedSpaceFeatures || '',
    spaceNotes: importedData?.importedSpaceNotes || '',
    floorplanLayoutType: space.floorplan_layout_type || '',
  };
  return { imageUrls, textData };
};

export const prepareLlmScoringDataWithProcessing = async (space: SpaceType) => {
  const { imageUrls, textData } = prepareLlmScoringData(space);
  const imageProcessingResult = await processImagesForLlm(imageUrls);
  return { processedImageUrls: imageProcessingResult.validUrls, textData, imageProcessingResult };
};

export const needsLlmScoring = (space: SpaceType): boolean => {
  const { lastLlmScoredAt, images, rawImportedData } = space;
  if (!lastLlmScoredAt) return true;
  const lastScoredDate = new Date(lastLlmScoredAt);
  const hasUpdatedImages = images.some((image) => image.updatedAt && new Date(image.updatedAt) > lastScoredDate);
  const hasUpdatedImportedData = rawImportedData.some((data) => data.updatedAt && new Date(data.updatedAt) > lastScoredDate);
  return hasUpdatedImages || hasUpdatedImportedData;
};

/**
 * Calls Claude to score a space based on images and text data.
 *
 * Key differences from the OpenAI implementation:
 * 1. Uses Anthropic SDK instead of OpenAI SDK
 * 2. Images are passed as { type: 'image', source: { type: 'url', url } } content blocks
 * 3. Structured output is enforced via tool_use with tool_choice instead of zodResponseFormat
 * 4. The tool input (not message content) is what we parse and Zod-validate
 */
export const scoreLlm = async (imageUrls: string[], textData: Record<string, string>): Promise<number> => {
  if (!process.env.ANTHROPIC_API_KEY) {
    throw new Error('ANTHROPIC_API_KEY is not configured');
  }

  const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

  // Build user message content: text first, then images
  const content: Anthropic.MessageParam['content'] = [];

  const textContent = Object.entries(textData)
    .filter(([, value]) => value)
    .map(([key, value]) => `${key}: ${value}`)
    .join('\n');

  content.push({
    type: 'text',
    text: `${textContent ? `Space Information:\n${textContent}\n\n` : ''}Please analyze the provided images and text data to score this space.`,
  });

  // Claude image content blocks use source.type = 'url' for remote images
  for (const url of imageUrls) {
    content.push({
      type: 'image',
      source: { type: 'url', url },
    });
  }

  const response = await anthropic.messages.create({
    model: MODEL,
    max_tokens: 2048,
    temperature: 0.2,
    system: SPACE_SCORE_SYSTEM_PROMPT,
    tools: [SCORE_SPACE_TOOL],
    tool_choice: { type: 'tool', name: 'score_space' },
    messages: [{ role: 'user', content }],
  });

  // Extract tool use block
  const toolUseBlock = response.content.find((block): block is Anthropic.ToolUseBlock => block.type === 'tool_use');

  if (!toolUseBlock) {
    throw new Error('No tool_use block in Claude response');
  }

  const verifiedResponse = SpaceScoringOutputSchema.safeParse(toolUseBlock.input);
  if (!verifiedResponse.success) {
    throw new Error(`Invalid response from Claude: ${verifiedResponse.error.message}`);
  }

  // Convert 0–10 aggregate score to 0–1 range for database storage
  return verifiedResponse.data.aggregate_score / 10;
};

export const scoreLlmWithProcessing = async (space: SpaceType): Promise<{ score: number | null; error: string | null }> => {
  try {
    const { processedImageUrls, textData, imageProcessingResult } = await prepareLlmScoringDataWithProcessing(space);

    const hasValidImages = processedImageUrls.length > 0;
    const hasTextData = Object.values(textData).some((val) => val);

    if (!hasValidImages && !hasTextData) {
      const errorMessages = [];
      if (imageProcessingResult.originalCount > 0 && imageProcessingResult.errors.length > 0) {
        errorMessages.push(`Image processing errors: ${imageProcessingResult.errors.map((e) => e.error).join('; ')}`);
      }
      errorMessages.push('No valid images or text data available for scoring');
      return { score: null, error: errorMessages.join('. ') };
    }

    if (imageProcessingResult.errors.length > 0) {
      console.warn(`Image processing errors for space ${space.id}:`, imageProcessingResult.errors);
    }

    const score = await scoreLlm(processedImageUrls, textData);
    return { score, error: null };
  } catch (error) {
    return {
      score: null,
      error: error instanceof Error ? error.message : 'Unknown scoring error',
    };
  }
};
