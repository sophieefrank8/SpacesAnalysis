# Urgent Space Confirmations Bot

Posts to `#urgent-space-confirmations` whenever demand ops creates a new
`ACTIVE_SEARCH_DEMAND` suggested outreach in the Neon DB, tagging the right
supply team member based on space city and lease type.

---

## One-Time Setup

### 1. Create the Slack App

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name: `Urgent Confirmations` — Workspace: **Tandem**
3. Under **OAuth & Permissions → Scopes → Bot Token Scopes**, add:
   - `chat:write`
   - `chat:write.public`
4. Click **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-...`)
5. In Slack, invite the bot to the channel: `/invite @UrgentConfirmations`

### 2. Configure Environment Variables

```bash
cp .env.example .env
# Then edit .env with your Neon connection string and Slack bot token
```

### 3. Install Python Dependencies

```bash
pip install psycopg2-binary slack_sdk python-dotenv
```

---

## Running the Bot

**Manual (one-off):**
```bash
cd /path/to/SpacesAnalysis
python UrgentConfirmations/slack_bot.py
```

**Automated (cron every 5 minutes):**

On Mac/Linux, open crontab:
```bash
crontab -e
```
Add this line (update the path):
```
*/5 * * * * cd /path/to/SpacesAnalysis && python UrgentConfirmations/slack_bot.py >> UrgentConfirmations/bot.log 2>&1
```

On **Windows**, use Task Scheduler:
- Action: `python C:\Users\Sophie\Documents\SpacesAnalysis\UrgentConfirmations\slack_bot.py`
- Trigger: Repeat every 5 minutes indefinitely

---

## Routing Logic

| Condition | Tagged |
|-----------|--------|
| `leaseType = T2T` | Sophie |
| NYC region, non-T2T | Allegra |
| SF region (not South SF), non-T2T | Pete |
| Boston region, non-T2T | Ian |
| Unknown city | Sophie (fallback) |

**Freshness flag:** If a space is CoStar/LoopNet-sourced, has a freshness check scheduled, and hasn't been updated in 30+ days — Sophie is also tagged and asked to check CoStar status first.

---

## State Tracking

`state.json` is auto-created on first run and stores the timestamp of the last
processed outreach. The bot only processes records created *after* this timestamp,
so it won't send duplicate messages on repeated runs.

To reset and reprocess recent records:
```bash
rm UrgentConfirmations/state.json
```

---

## Known Limitation

`suggested_outreaches` currently has no structured mate/company ID. The `note`
field (free text entered by demand ops) is used as-is for "Searching for."
Once the Output 2 Geremy ticket adds a `mate_id` column, the bot can be updated
to show the structured company name.
