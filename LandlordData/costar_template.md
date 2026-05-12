# CoStar Enrichment Template — Quarterly Landlord Research

After Claude generates the target building list and owner research (`[market]_research.md`), each territory rep enriches the list using CoStar before the final report is sent.

**Who does what:**
- Peter Sellick → SF CoStar enrichment
- Allegra Citak → NYC CoStar enrichment
- Ian Ostberg → Boston CoStar enrichment

**Output file:** Save your enrichment as `quarterly/YYYY-QN/[market]_costar.csv`
**Column order must match exactly** (see header row below).

---

## CSV Header

```
address,legal_owner_confirmed,asset_manager_name,asset_manager_email,asset_manager_phone,portfolio_size,asking_rent,recent_transactions,other_markets,submarket_vacancy,leasing_broker
```

One row per building. The `address` column must match the address in `[market]_targets.csv` exactly so rows merge correctly.

---

## What to Look Up Per Building

### 1. Legal Owner (`legal_owner_confirmed`)
The entity listed in CoStar's owner field. Often differs from what's on Tandem (shell LLCs, trust names, etc.).
- Example: "1499 Illinois LLC" vs "Ronaldo Cianciarulo / RJC Group"
- If CoStar matches Tandem, confirm with "Confirmed: [name]"

### 2. Asset Manager / Property Manager (`asset_manager_name`, `asset_manager_email`, `asset_manager_phone`)
The person who handles day-to-day leasing decisions. This is often a better first contact than the legal owner, especially for buildings with professional management.
- Look under: Contacts tab → Property Manager or Asset Manager
- Include direct email and direct phone if shown

### 3. Portfolio Size (`portfolio_size`)
How many properties and/or total square footage this owner manages nationally.
- Format: "8 properties, ~420,000 SF" or "3 buildings in SF only"
- Use this to validate the small/medium classification from Phase 2

### 4. Asking Rent (`asking_rent`)
Current asking rent at this specific building.
- Format: "$42–$55/SF/yr NNN" or "$65/SF/yr gross"
- Use the most recent available listing or comparable

### 5. Recent Transactions (`recent_transactions`)
Any sale, acquisition, or refinancing in the last 3 years.
- Format: "Sold Jan 2025 for $14.2M" or "Refinanced Q3 2024" or "No recent activity"
- New ownership = warm outreach opportunity (they may want to refresh tenant mix)
- Recent sale = may have new decision-maker; verify contacts

### 6. Other Markets (`other_markets`)
Does this owner hold assets in other cities? Cross-reference CoStar portfolio view.
- Format: "SF only" or "SF (3), NYC (1)" or "National — 12 markets"

### 7. Submarket Vacancy (`submarket_vacancy`)
Current vacancy rate for the neighborhood/submarket (not the building, the submarket).
- Format: "14.2% (Flatiron submarket, Q1 2026)"
- High vacancy = owner may be more motivated to activate flex

### 8. Leasing Broker (`leasing_broker`)
If the owner uses an exclusive leasing broker for this building, who is it?
- Format: "John Smith at CBRE (jsmith@cbre.com)" or "Direct — no exclusive broker"
- If there's a broker, Tandem outreach should go to the broker first, not the owner

---

## Tips

- **CoStar → Property → Contacts** is the most reliable place for asset manager info
- **CoStar → Property → Sale History** for recent transactions
- **CoStar → Owner → Portfolio** to assess total holdings
- If CoStar shows a property management company (e.g., Cushman, JLL) rather than a direct owner contact, that's the leasing broker — note it
- Leave a cell blank rather than guessing; the report will flag it as "⚠ CoStar" for follow-up
- If you can't find data for a field after 5 minutes of looking, move on — partial enrichment is better than delay

---

## Example Row

```
655 3rd St suite 415,Samuelson Schafer LP,James Schafer,jim@ssproperty.us,(415) 435-2983,"3 properties, ~180000 SF",$58/SF/yr NNN,No recent activity,SF only,11.4% (Mission Bay Q1 2026),Direct — no exclusive broker
```
