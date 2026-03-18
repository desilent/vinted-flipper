# Vinted Flipper 🏷️

AI-powered Vinted resale price evaluator. Paste a listing URL, get instant pricing analysis with real comparable listings and retail price lookup.

## What it does

1. **Paste a Vinted URL** → extracts item details (brand, price, condition) directly from Vinted's API
2. **Finds comparable listings** → searches Vinted for 10-20 similar items with real prices
3. **Finds retail price** → uses AI (Claude) to search the web for the original retail price
4. **Calculates everything** → profit margins, ROI, buy/sell recommendations, verdict

## Setup on Unraid

### Step 1: Get an Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an account and add $5 credit (each lookup costs ~$0.01-0.02)
3. Go to **API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-...`)

### Step 2: Deploy on Unraid

**Option A: Using Docker Compose (recommended)**

1. Copy this entire `vinted-flipper` folder to your Unraid server (e.g. `/mnt/user/appdata/vinted-flipper/`)
2. Edit the `.env` file and paste your API key
3. SSH into Unraid and run:

```bash
cd /mnt/user/appdata/vinted-flipper
docker-compose up -d
```

4. Access at `http://YOUR-UNRAID-IP:8444`

**Option B: Using Unraid's Docker UI**

1. Build the image first:
```bash
cd /mnt/user/appdata/vinted-flipper
docker build -t vinted-flipper .
```

2. In Unraid UI → Docker → Add Container:
   - **Name:** vinted-flipper
   - **Repository:** vinted-flipper
   - **Port:** 8444 → 8080
   - **Variable:** ANTHROPIC_API_KEY = `your-key-here`
   - **Variable:** VINTED_DOMAIN = `de`

### Step 3: Access from any device

Open `http://YOUR-UNRAID-IP:8444` on your phone or computer. Share this URL with your partner — they can use it from any device on the same network.

**Want access outside your home?** Set up a Cloudflare Tunnel or use Unraid's built-in reverse proxy.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required) | Your Anthropic API key |
| `VINTED_DOMAIN` | `de` | Vinted country: de, fr, nl, co.uk, it, es, etc. |

## Costs

- **Vinted data:** Free (uses their internal API)
- **AI retail price lookup:** ~$0.01-0.02 per query
- **$5 credit** will last you ~250-500 lookups

## Troubleshooting

- **"Could not fetch item"** — Vinted may have temporarily blocked the request. Wait a minute and try again. At your usage volume this should be rare.
- **No retail price found** — The AI couldn't find a reliable price. You can enter it manually.
- **Cookie issues** — The app auto-refreshes Vinted session cookies. If items consistently fail, restart the container.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│   Browser    │────▶│  FastAPI Backend  │────▶│  Vinted API │
│  (frontend)  │◀────│   (Python)        │◀────│  (internal)  │
└─────────────┘     │                  │     └─────────────┘
                    │                  │     ┌─────────────┐
                    │                  │────▶│ Anthropic AI │
                    │                  │◀────│ (retail $$$) │
                    └──────────────────┘     └─────────────┘
```
