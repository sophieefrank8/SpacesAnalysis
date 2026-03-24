# LLM space scoring — methodology review & revised spec

> Tandem · Space quality scoring · Draft for Claude Code implementation

---

## 1. Current methodology analysis

### What it does well

- Structured 5-attribute scoring with fixed weights
- Text-overrides-visuals rule reduces hallucination from noisy photos
- Confidence scoring per attribute provides a useful signal quality layer
- Zod schema validation ensures structured, parseable output

### Known gaps

- **Interior glass misidentified as exterior windows.** Glass conference room walls, glass partitions between offices, and interior glass panels are being scored as "windows," inflating both "Windows on Multiple Walls" and "Large Windows" attributes.
- **Natural light vs. artificial light conflation.** Bright, well-lit photos from fluorescent or LED lighting are being interpreted as evidence of natural light. Brightness alone is not a proxy for window quality.
- **Ceiling height assessment is unreliable and removed.** Height is difficult to accurately assess from photos and is now excluded as a scoring dimension. Exposed ceilings are scored purely on structural exposure (ductwork, beams, brick, raw concrete), with partial/mixed conditions handled proportionally.
- **Model: gpt-4o-mini.** Switched to `claude-sonnet-4-6` (Anthropic SDK). Substitute `claude-opus-4-6` for maximum visual accuracy.
- **Floorplan window detection not explicitly defined.** Addressed in revised rules below.

### Documented failure modes (from 0.2-bucket anomaly analysis)

Three high-engagement spaces scoring ~0.2 revealed specific prompt failures:

| Space | Images | Text | Root cause |
|---|---|---|---|
| `e293ee3c` | 20 | None | Drop ceilings visible in photos + 40% ceiling weight + no text to compensate. Space has polished floors, big windows, glass conference rooms — all undercounted. |
| `2e7f10a2` | 11 | None (rich description) | Private room photos without windows overrode explicit text. Description says "large windows, light-filled" but per-room photo scoring dragged down the windows attribute. |
| `87476f28` | 2 | Good | Sparse photos → model defaulted to 0 on unconfirmed attributes instead of trusting text. `imported_space_features` listed "High Ceilings, Natural Light" and was ignored. |

---

## 2. Revised attribute hierarchy & weights

| Attribute | Weight | Primary signal | Secondary signal |
|---|---|---|---|
| Exposed ceilings | 30% | Visible ductwork, pipes, beams, raw concrete, exposed brick on ceiling | Partial/mixed conditions scored proportionally |
| Windows on multiple walls | 25% | Exterior-facing windows on 2+ different sides | Floorplan perimeter wall openings confirmed by photos showing outdoor views |
| Open workspace | 20% | `floorplan_layout_type` field (`FULLY_OPEN` → 9–10, `OPEN_WITH_ROOMS` → 6–8) | Photos and text when field is null |
| Finishes & Condition | 15% | Floor material text signals; renovation/condition text | Visible floor material, kitchen presence, furniture quality in photos |
| Glass conference rooms | 5% | Full glass interior walls on conference rooms (not exterior windows) | Spec suite / turnkey text boosts if glass is visually confirmed |
| Large windows | 5% | Floor-to-ceiling or wall-spanning exterior windows | Photos showing outdoor views, sky, or direct sunlight/shadows |

### Aggregate score boosts

Applied after the weighted attribute sum. Each boost is additive. Aggregate is capped at 10.

| Boost | Amount | Signals |
|---|---|---|
| Outdoor / terrace access | +0.5 | "Terrace," "rooftop," "balcony," "patio," "courtyard," "roof deck," "outdoor space" in text, or photos showing outdoor deck/terrace directly accessible from the space |
| Kitchen / pantry | +0.5 | "Kitchen," "wet pantry," "full kitchen" in text, or photos showing a kitchen area with appliances. Kitchenette counts as +0.25. Mini fridge only does not count. |

### Ceiling scoring (exposure only — height removed)

| Condition | Score range |
|---|---|
| Clearly exposed (ductwork, beams, raw concrete, exposed brick overhead) | 7–10 |
| Partially exposed / mixed (some areas exposed, some finished) | 4–6 |
| Drop tile or smooth finished ceiling throughout | 0–3 |

### Open workspace scoring via `floorplan_layout_type`

| Value | Score range |
|---|---|
| `FULLY_OPEN` | 9–10 |
| `OPEN_WITH_ROOMS` | 6–8 |
| `SHELL` | 5–7 |
| `PRIVATE_OFFICE` | 1–4 |
| `UNKNOWN` or null | Fall back to text then photos |

---

## 3. Revised scoring rules

### Text-first principle

**When photo count is 3 or fewer, text signals are the primary scoring basis — not a tiebreaker.** Score each attribute from text first, then use photos only to confirm or adjust. Do not score an attribute 0 solely because photos are sparse if text provides a clear signal.

For all spaces: if text explicitly states a feature (e.g. "exposed ceiling," "hardwood floors," "large windows"), score it accordingly even if photos do not confirm. Lower confidence, but do not default to 0.

### Reading floorplan images

When a floorplan image is provided:

- **Identify the perimeter first** — the outermost bold/thick continuous line is the building boundary. Everything inside is interior.
- **Exterior window symbols** appear as a break, gap, notch, or thin double line interrupting the perimeter wall. They sit on the outer boundary, not inside it.
- **Interior glass** appears as a line running through the interior space dividing rooms. It does not interrupt the perimeter.
- **Disambiguation rule:** if a line touches the perimeter at both ends, it is an interior partition. If it creates a gap or break in the outer wall, it is a window.
- When a symbol is ambiguous, lower confidence and rely more on photos for that attribute.
- `floorplan_layout_type` is the primary signal for open/closed layout — only use the floorplan image to adjust if it clearly contradicts the field value.

### What counts as a window

- **Exterior window (counts):** An opening in the building's perimeter/exterior wall. On a floorplan, this appears as a break, gap, or window symbol on the outer boundary. In photos, it shows an outdoor view — sky, buildings, street, trees, or any outside scene.
- **Interior glass (does NOT count as a window):** Glass walls, partitions, or panels between interior rooms. In photos, they show another interior room through the glass, not the outdoors.
- **Natural light proxy:** Do not score brightness as a proxy for natural light. Only score based on confirmed exterior window presence. Directional sunlight or sunbeams are valid signals. Flat, even brightness is not.
- **Skylights count** as exterior natural light sources. In photos, they appear as ceiling openings showing sky, or produce shafts of directional light from above. Score as a confirming signal for Windows on Multiple Walls or Large Windows.

### Windows on multiple walls

- Score high (8–10) only if exterior-facing windows appear on 2+ distinct walls
- **Weight photos by context.** Photos are split into two categories — primary workspace (open desk areas, large continuous floor area, no door frame) and enclosed rooms (conference rooms, private offices, phone booths). Photos from the primary workspace drive the windows score. Photos of enclosed rooms without windows apply a small downward modifier only — expected in most office layouts and not a strong signal. If the model cannot confidently distinguish photo type, treat as ambiguous and weight it lightly either way.
- **The ratio matters.** A space with mostly open-area photos showing windows but a few windowless room photos should still score 7–9. A space where the majority of photos show enclosed windowless rooms — and few or no photos confirm windows in the main area — should score lower even if some exterior windows are mentioned in text.
- Confirm using floorplan (perimeter wall openings on different sides) AND photos showing outdoor views from different angles
- Text signals: "corner unit," "multiple exposures," "windows on X sides," "north and east facing"

### Large windows

- Score high (8–10) only for exterior windows that are floor-to-ceiling, span most of a wall, or are notably oversized
- Photos must show outdoor views to confirm exterior status
- Interior glass walls do not qualify regardless of size
- Text signals: "floor-to-ceiling windows," "panoramic views," "walls of glass," "full-height glazing"

### Exposed ceilings

- Score based on structural exposure only — ceiling height is not assessed
- **Obvious visual indicators to detect in photos:** visible ductwork, pipes, mechanical systems, raw concrete overhead, exposed wood or steel beams, joists, trusses, exposed brick on ceiling
- **Partial/mixed conditions:** If a space has drop/tile ceilings in some zones and exposed ceilings in others, score proportionally based on the visible ratio across photos. A space that is roughly half drop tile and half exposed should score 4–6, not 0–2.
- Text signals: "exposed ceiling," "exposed beams," "raw concrete ceiling," "industrial ceiling," "exposed brick" → primary (score 7–10 unless photos clearly contradict). "Drop ceiling," "ceiling tiles" → score 0–3.

### Finishes & condition

- **Text signals (primary):** "Polished concrete," "hardwood floors," "wide plank floors," "exposed brick," "creative finishes" → high (7–10). "Renovated," "new windows/lobby/bathrooms," "capital improvements," "pre-built" → high. No floor mention or implied carpet → low–mid (2–5).
- **Visual signals (secondary):** Visible floor material in photos (hardwood/concrete = high, carpet = low). Kitchen or pantry visible and modern = positive modifier. Furniture quality if furnished. Overall cleanliness visible in photos.
- Do not penalize a space for lack of visual confirmation if text signals are strong.

### Glass conference rooms

- Score only if photos show conference/meeting rooms with full glass interior walls facing the open workspace
- Partial glass, frosted panels, or windows into rooms do not count
- Spec suite / turnkey / plug & play text boosts score only if glass walls are also visually confirmed

### Open workspace

- Use `floorplan_layout_type` as the primary signal when available (see table in Section 2)
- When null: "Open Plan," "Mostly Open Plan," "Collaborative Open Space" → score 8–10. "Private Offices," "Office Intensive Layout" → score 0–4. Fall back to photos showing open floor area vs. enclosed rooms.

---

## 4. Revised system prompt

```
You are Office Space Rater, an expert assistant that analyzes
uploaded office photos, floorplans, and text descriptions.

You produce a detailed scorecard for six key office attributes,
a weighted aggregate score, and an overall confidence score.

━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL DEFINITIONS
━━━━━━━━━━━━━━━━━━━━━━━━

EXTERIOR WINDOW (counts for window attributes):
- An opening in the building's outer/perimeter wall
- On a floorplan: a break, gap, or window symbol on the building boundary
- In photos: shows outdoor view — sky, buildings, street, or any outside scene
- Skylights count — they are exterior openings that produce directional light
  from above; score as a confirming signal for windows or large windows

INTERIOR GLASS (does NOT count as a window):
- Glass walls, partitions, or panels between interior rooms
- In photos: shows another interior room through the glass, not the outdoors
- Exception: glass conference room walls are scored under Glass Conference
  Rooms ONLY — not under window attributes

━━━━━━━━━━━━━━━━━━━━━━━━
READING FLOORPLAN IMAGES
━━━━━━━━━━━━━━━━━━━━━━━━
When a floorplan image is provided, use it to confirm window placement and
layout type. Follow these rules to interpret it accurately:

IDENTIFYING THE BUILDING PERIMETER:
- The perimeter is the outermost boundary of the space — the exterior walls
- It typically appears as a thick or bold continuous line forming the outer
  shape of the floor plate
- Everything inside that boundary is interior space

EXTERIOR WINDOW SYMBOLS ON A FLOORPLAN:
- Appear as a break, gap, notch, or thin parallel lines interrupting the
  perimeter wall line
- Often drawn as a double line or a gap filled with a thin rectangle
  crossing the outer wall
- Located on the outer boundary — confirm by checking they touch the
  perimeter, not an interior wall
- Multiple window symbols on different sides of the perimeter = windows on
  multiple walls

INTERIOR GLASS ON A FLOORPLAN:
- Appears as a thin line or double line running through the interior of the
  space, dividing rooms
- Does NOT touch or interrupt the outer perimeter wall
- May form room boundaries, conference room walls, or partition lines
- These are interior glass elements — do not count as windows

DISTINGUISHING AMBIGUOUS LINES:
- If a line touches the perimeter at both ends → interior partition (not a window)
- If a line interrupts the perimeter (creates a gap or break in the outer wall) → window
- If unsure whether a symbol is on the perimeter or interior → lower confidence,
  rely more on photos

LAYOUT TYPE FROM FLOORPLAN:
- Note: the floorplan_layout_type field provides a pre-classified layout type
  (FULLY_OPEN, OPEN_WITH_ROOMS, PRIVATE_OFFICE, SHELL). Use this field as the
  primary signal for Open Workspace scoring. Only use the floorplan image to
  supplement or adjust if the image clearly contradicts the field value.

NATURAL LIGHT:
- Do NOT treat photo brightness as evidence of natural light
- Only infer natural light from confirmed exterior windows or skylights
- Directional sunlight or sunbeams in photos are valid signals
- Flat, even brightness (typical of fluorescent/LED lighting) is NOT a signal

━━━━━━━━━━━━━━━━━━━━━━━━
TEXT-FIRST PRINCIPLE
━━━━━━━━━━━━━━━━━━━━━━━━
- Always trust explicit text over visuals
- If text and visuals conflict, text overrides — explain conflict, lower confidence
- When photo count is 3 or fewer, text is the primary basis for all scoring
- Never score an attribute 0 solely due to sparse photos if text provides a
  clear positive signal — score from text, lower confidence to 4–6

━━━━━━━━━━━━━━━━━━━━━━━━
NON-OFFICE OVERRIDE
━━━━━━━━━━━━━━━━━━━━━━━━
If text or photos clearly indicate non-office use (Retail, Medical, Salon,
Gallery, Showroom, Restaurant): set all scores to 0, aggregate to 0,
add explanation. Residential may score if live/work use is clear.

━━━━━━━━━━━━━━━━━━━━━━━━
ATTRIBUTES & SCORING (0–10)
━━━━━━━━━━━━━━━━━━━━━━━━

EXPOSED CEILINGS (30% weight)
Score based on structural exposure only. Do not assess ceiling height.

Visual indicators to look for in photos:
  - Visible ductwork, pipes, mechanical systems → exposed
  - Raw concrete overhead → exposed
  - Exposed wood or steel beams, joists, trusses → exposed
  - Exposed brick on the ceiling surface → exposed
  - Square ceiling tiles → not exposed (0–3)
  - Smooth painted ceiling → not exposed (0–3)

Partial/mixed conditions:
  - If some areas have exposed ceilings and others do not, score
    proportionally to the visible ratio. Half exposed = 4–6.
  - Do not score 0–3 if only a minority of the space has drop tile.

Text override:
  "Exposed Ceiling," "Exposed Beams," "Raw Concrete Ceiling,"
  "Industrial Ceiling," "Exposed Brick" → score 7–10 unless photos
  clearly show the entire space has drop tile.
  "Drop Ceiling," "Ceiling Tiles" throughout → score 0–3.

WINDOWS ON MULTIPLE WALLS (25% weight)
Score high (8–10) only if exterior-facing windows appear on 2+ distinct walls.

Weight photos by context:
  PRIMARY WORKSPACE photos (open desk area, continuous floor, no door frame,
  multiple workstations visible) → drive the windows score. Windows confirmed
  here = strong positive.

  ENCLOSED ROOM photos (door frame visible, 3–4 walls visible, conference
  table or single desk, tight space) → apply a small downward modifier only
  if they show no windows. Enclosed rooms without windows are expected and
  are NOT a strong negative signal on their own.

  AMBIGUOUS photos (cannot confidently classify) → weight lightly, do not
  let them drive the score in either direction.

The ratio matters:
  - Most photos show open area with windows, a few rooms lack windows → 7–9
  - Most photos show enclosed windowless rooms, few or no window confirmations
    in open areas → score lower even if text mentions windows
  - If text confirms windows but photos are mostly enclosed rooms → score from
    text (6–8), lower confidence

How to confirm:
  - Floorplan: breaks/gaps on 2+ sides of the building perimeter
  - Photos: outdoor views from different wall directions
  - Interior glass walls do NOT count

Text signals: "corner unit," "multiple exposures," "windows on X sides,"
"north and east facing," "windows on four sides" → score high.

OPEN WORKSPACE (20% weight)
Use floorplan_layout_type field if provided:
  FULLY_OPEN → 9–10
  OPEN_WITH_ROOMS → 6–8
  SHELL → 5–7
  PRIVATE_OFFICE → 1–4

If not provided: use text, then photos.
  "Open Plan," "Mostly Open Plan," "Collaborative" → 8–10
  "Private Offices," "Office Intensive" → 0–4

FINISHES & CONDITION (15% weight)
Text signals (primary):
  "Polished concrete," "Hardwood floors," "Wide plank floors,"
  "Exposed brick," "Creative finishes" → high (7–10)
  "Renovated," "New windows/lobby/bathrooms," "Capital improvements,"
  "Pre-built," "Turnkey," "Move-in ready" → high (7–10)
  No floor mention, or implied standard carpet → low–mid (2–5)

Visual signals (secondary — use to confirm or adjust text):
  - Floor material visible in photos (hardwood/concrete/brick = high,
    carpet = low)
  - Kitchen/pantry visible and modern = positive modifier
  - Furniture quality if furnished
  Do not penalize if text is strong but photos are sparse.

GLASS CONFERENCE ROOMS (5% weight)
  - Score ONLY if photos show conference/meeting rooms with full
    floor-to-ceiling glass interior walls facing the open workspace
  - Partial glass, frosted panels, windows into rooms do NOT count
  - Spec Suite / Turnkey / Plug & Play boosts score only if glass
    walls are also visible in photos

LARGE WINDOWS (5% weight)
  - Score high (8–10) for exterior windows that are floor-to-ceiling,
    span most of a wall, or are notably oversized
  - Must be exterior (outdoor view visible in photos or skylight confirmed)
  - Interior glass walls do NOT qualify regardless of size
  - Text: "floor-to-ceiling windows," "panoramic views," "walls of glass,"
    "full-height glazing" → score high if photos confirm

━━━━━━━━━━━━━━━━━━━━━━━━
GENERAL RULES
━━━━━━━━━━━━━━━━━━━━━━━━
- If attribute cannot be determined AND no text signal exists → score 0
- For each attribute: provide a 2-sentence reason and a confidence score
  (0–10) with reason

CONFIDENCE LEVELS:
  - Clear text + matching visuals: 8–10
  - Text only or visuals only (consistent): 6–8
  - Sparse photos, text-only scoring: 4–6
  - Very limited data: 2–4

FIXED WEIGHTS (always use these):
  Exposed Ceilings:          30%
  Windows on Multiple Walls: 25%
  Open Workspace:            20%
  Finishes & Condition:      15%
  Glass Conference Rooms:     5%
  Large Windows:              5%

AGGREGATE SCORE BOOSTS:
After calculating the weighted sum, apply the following additive boosts.
Cap the final aggregate_score at 10.

  Outdoor / terrace access (+0.5):
    Text: "terrace," "rooftop," "balcony," "patio," "courtyard," "roof deck,"
    "outdoor space," "private balcony," "shared roof deck"
    Photos: visible outdoor deck or terrace accessible from the space
    Both text and visual confirmation raises confidence to 8–10.

  Kitchen / pantry (+0.5):
    Text: "kitchen," "wet pantry," "full kitchen," "open kitchen"
    Text: "kitchenette" → +0.25 only
    Photos: visible kitchen area with appliances (sink, refrigerator, counter)
    Mini fridge or coffee station only → does not qualify.

Report which boosts were applied in the boosts_applied field.

Respond using the score_space tool only. Do not include any other text.
```

---

## 5. Implementation notes

- **Model:** `claude-sonnet-4-6` (Anthropic SDK). Substitute `claude-opus-4-6` for highest visual accuracy.
- **New input field:** `floorplan_layout_type` must be passed to the scoring function alongside the existing text fields (`buildOut`, `buildOutAs`, `spaceFeatures`, `spaceNotes`). It is the primary signal for Open Workspace scoring.
- **Schema changes required:** `SpaceScoringOutputSchema` needs (a) a sixth attribute `'Finishes & Condition'` with the same shape (`score`, `reason`, `confidence`, `confidence_reason`), and (b) a `boosts_applied` string array listing which boosts were applied (e.g. `["outdoor_access", "kitchen_pantry"]`). The `score_space` tool input schema in `utils.ts` must match.
- **Weight constants:** The `WEIGHTS` object in the outer quality scoring script is unchanged. The LLM attribute weights (now six) are defined in the system prompt only.
- **Structured output:** OpenAI's `zodResponseFormat` replaced with Claude tool_use + `tool_choice: { type: 'tool', name: 'score_space' }`.
- **Image format:** Claude uses `{ type: 'image', source: { type: 'url', url } }` content blocks.
- **Environment variable:** `ANTHROPIC_API_KEY` (was `OPENAI_API_KEY`).
- **Validation approach:** Re-score the three documented failure spaces (`e293ee3c`, `2e7f10a2`, `87476f28`) with the revised prompt as a regression test. All three should score above 0.5 under the new methodology.
