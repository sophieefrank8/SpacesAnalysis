# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Who Sophie Is

Sophie is a PM at Tandem, reporting directly to the CEO (Rafi). Her work spans three areas:

1. **Product** — proposing and thinking through new features for the Tandem website (git repo: `tandem-space`)
2. **Supply operations** — helping the supply team get more spaces published and searchable faster, with better data quality
3. **HQ program** — managing Tandem's co-working program (currently with landlords BAM and BH in San Francisco), targeted at YC founders as a top-of-funnel motion toward private office conversion

When assisting Sophie, default to outputs she can share directly with Rafi or her teammates — clear, confident, and synthesis-first.

---

## WAT Framework

Claude should operate within the **Workflows, Agents, Tools** framework:

- **Workflows first** — before writing code or queries, map out the process end-to-end. How does data flow? Where are the handoffs? What breaks?
- **Tools second** — assess what already exists (skills, scripts, MCP connections) before building anything new. Improve existing tools rather than duplicating them.
- **Agent behavior** — act autonomously within a task, but surface decisions and tradeoffs clearly so Sophie can steer.

Spend most effort in the planning and communication layer — how a system works and how to explain it to the team — not just execution.

---

## Output Format

Every substantive response should include:

1. **Logic** — what query, process, or reasoning was used
2. **2–5 sentence summary** — plain language synthesis Sophie can share with Rafi or teammates
3. **Suggested next steps** — what to do with the output, flag, or investigate

Use tables for data output. Keep summaries confident and direct — no hedging unless data genuinely doesn't support a conclusion.

Outputs are shared via **Slack** (quick updates, team-facing) or **Notion** (longer reports, structured documentation).

---

## Key Systems — Always in Context

Both of these should inform every query, analysis, and feature discussion:

| System | Purpose |
|--------|---------|
| **Neon DB** (`sparkling-feather-59720473`) | Primary data warehouse — use `/neon-db` skill for all queries |
| **`Tandem-Space/tandem` GitHub repo** | Tandem website codebase — reference for feature ideas, current functionality, and implementation context. Accessible via GitHub MCP (`owner: Tandem-Space`, `repo: tandem`). |

When proposing features, cross-reference what already exists in the `tandem` repo using the GitHub MCP. When pulling data, use the `/neon-db` skill.

**MCP access is read-only for both systems.** Never use write operations via either MCP:
- **GitHub MCP** — read-only. Never create branches, push files, create/merge PRs, or modify anything in the repo.
- **Neon MCP** — read-only. Never create/delete projects or branches, run migrations, or use any tool that modifies infrastructure.

---

## Supply Team Context

The supply team works to get spaces from imported → published + searchable. The key constraint: **a space cannot go live without explicit sign-off from both the broker and the landlord/owner**.

This broker/landlord permission dynamic is the primary friction point in the supply pipeline. When analyzing supply metrics or designing workflows, always account for this dependency.

**Territory assignments:**
| Team Member | Territory |
|-------------|-----------|
| Allegra Citak | NYC |
| Peter Sellick | SF |
| Ian Ostberg | Boston |

- **Public/live space:** `status = 'PUBLISHED' AND "isSearchable" = true`
- Supply activity is tracked via the `opportunities` table (CRM for broker/landlord outreach)

**Pipeline health signals:**
- An opportunity in the `engaging` stage for **more than 30 days** is a concern — flag for follow-up
- Track number of `opportunity_event` entries required to move from `engaging` → `contributing` as an efficiency metric

---

## HQ Program Context

HQ is a co-working program run at landlord-partner locations (currently BAM and BH in San Francisco). It targets YC founders. The strategic goal is conversion: HQ member → Tandem private office tenant.

When working on HQ-related analysis, always frame around conversion rate and pipeline movement from HQ → private deal.

**HQ space IDs (use these to identify HQ members via `match`):**
- `3f87ec9f-e0a3-4026-ab18-1c30ac644856`
- `59f764d4-107d-47a8-b203-481342055609`
- `26d70b51-d81d-41c6-a04e-928c12138728` (989 Market St, SF — BH Properties)

HQ tenants = companies with a live or past match on one of these spaces. Conversion = same company later appears as a `mateId` on a non-HQ private office match.

---

## Neon DB Rules

- **Read-only** — SELECT only. Never INSERT, UPDATE, DELETE, DROP, or any write operation.
- Always apply fee normalization: `FEE_ONE_TIME_FOR_TERM` → divide `feeAmount` by `term`
- Always apply deal modifications from `retool_closed_deal_modifications` for accurate GMV/rent
- Use `ILIKE` for city filters (mixed case in DB). Exclude `'South San Francisco'` when filtering for SF
- Most columns are camelCase and require double quotes in SQL — see the `/neon-db` skill for full schema and snake_case exceptions

---

## Key Business Logic

- **Live deals:** `contractSignedAt IS NOT NULL AND proposedStartDate <= NOW() AND (moveOutDate IS NULL OR moveOutDate > NOW()) AND m.status IN ('MATCH_ACTIVATION','MATCH_LIVE','MATCH_RETIRED','CONTRACT_SIGNED','E_SIGNATURE')`
- **Active match statuses:** `MATCH_ACTIVATION`, `MATCH_LIVE`, `MATCH_RETIRED`, `CONTRACT_SIGNED`, `E_SIGNATURE`
- **New signups:** companies with a `space_requirements` row, excluding test/admin/duplicate/internal (`@tandem.space`) companies
- **Mate** = tenant; **Host** = landlord — both stored in the `companies` table
