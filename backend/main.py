import os
import re
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx

# ─── Logging ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vinted-flipper")

# ─── Config ───
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
VINTED_DOMAIN = os.getenv("VINTED_DOMAIN", "de")  # de, fr, co.uk, nl, etc.

# ─── Vinted client (lightweight, no external dependency needed) ───

class VintedClient:
    """Minimal Vinted API client using their internal v2 API."""

    def __init__(self, domain: str = "de"):
        self.domain = domain
        self.base_url = f"https://www.vinted.{domain}"
        self.api_url = f"{self.base_url}/api/v2"
        self.cookie = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        }

    async def _refresh_cookie(self, client: httpx.AsyncClient):
        """Fetch a fresh session cookie from Vinted."""
        try:
            resp = await client.get(self.base_url, headers={
                **self.headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }, follow_redirects=True)
            cookies = resp.cookies
            # Look for the access token cookie
            for name, value in cookies.items():
                if "access_token" in name or "_vinted_fr_session" in name or "session" in name.lower():
                    self.cookie = {name: value}
                    log.info(f"Got Vinted cookie: {name}=...{value[-10:]}")
                    return
            # If no specific cookie found, use all cookies
            self.cookie = dict(cookies)
            if self.cookie:
                log.info(f"Got {len(self.cookie)} cookies from Vinted")
            else:
                log.warning("No cookies received from Vinted")
        except Exception as e:
            log.error(f"Failed to get Vinted cookie: {e}")
            self.cookie = {}

    async def get_item(self, item_id: str) -> Optional[dict]:
        """Fetch a single item's details by ID."""
        async with httpx.AsyncClient(timeout=15) as client:
            if not self.cookie:
                await self._refresh_cookie(client)

            url = f"{self.api_url}/items/{item_id}?localize=false"
            try:
                resp = await client.get(url, headers=self.headers, cookies=self.cookie)
                if resp.status_code == 401 or resp.status_code == 403:
                    log.info("Cookie expired, refreshing...")
                    await self._refresh_cookie(client)
                    resp = await client.get(url, headers=self.headers, cookies=self.cookie)

                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("item", data)
                else:
                    log.warning(f"Item fetch failed: {resp.status_code}")
                    return None
            except Exception as e:
                log.error(f"Error fetching item {item_id}: {e}")
                return None

    async def search(self, query: str, brand_ids: list = None, catalog_ids: list = None, per_page: int = 20) -> list:
        """Search Vinted catalog."""
        async with httpx.AsyncClient(timeout=15) as client:
            if not self.cookie:
                await self._refresh_cookie(client)

            params = {
                "search_text": query,
                "per_page": str(per_page),
                "page": "1",
                "order": "relevance",
            }
            if brand_ids:
                for i, bid in enumerate(brand_ids):
                    params[f"brand_ids[]"] = str(bid)
            if catalog_ids:
                for i, cid in enumerate(catalog_ids):
                    params[f"catalog_ids[]"] = str(cid)

            url = f"{self.api_url}/catalog/items"
            try:
                resp = await client.get(url, params=params, headers=self.headers, cookies=self.cookie)
                if resp.status_code in (401, 403):
                    await self._refresh_cookie(client)
                    resp = await client.get(url, params=params, headers=self.headers, cookies=self.cookie)

                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("items", [])
                else:
                    log.warning(f"Search failed: {resp.status_code}")
                    return []
            except Exception as e:
                log.error(f"Search error: {e}")
                return []


# ─── AI retail price finder ───

async def find_retail_price(brand: str, title: str, description: str = "") -> dict:
    """Use Anthropic API to find the original retail price."""
    if not ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY set, skipping retail price lookup")
        return {"retail_price": None, "source": None, "notes": "No API key configured"}

    prompt = f"""Find the original retail price in EUR for this item:
Brand: {brand}
Title: {title}
{f'Description: {description[:300]}' if description else ''}

Search for the retail price of this specific item (or very similar model). 
Respond with ONLY a JSON object, no markdown, no backticks:
{{"retail_price": number_or_null, "source": "where you found it or null", "notes": "brief note about pricing"}}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

            if resp.status_code != 200:
                log.error(f"Anthropic API error: {resp.status_code} {resp.text[:200]}")
                return {"retail_price": None, "source": None, "notes": "API error"}

            data = resp.json()
            text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
            full_text = "\n".join(text_blocks)
            cleaned = re.sub(r"```json|```", "", full_text).strip()

            import json
            match = re.search(r"\{[^}]+\}", cleaned)
            if match:
                return json.loads(match.group(0))
            else:
                return {"retail_price": None, "source": None, "notes": "Could not parse AI response"}
    except Exception as e:
        log.error(f"Retail price lookup failed: {e}")
        return {"retail_price": None, "source": None, "notes": str(e)}


# ─── Helpers ───

def extract_item_id(url: str) -> Optional[str]:
    """Extract item ID from a Vinted URL."""
    match = re.search(r"/items/(\d+)", url)
    return match.group(1) if match else None

def extract_search_terms(url: str) -> str:
    """Extract search terms from URL slug."""
    match = re.search(r"/items/\d+-(.+?)(?:\?|$)", url)
    if match:
        return match.group(1).replace("-", " ").strip()
    return ""

def parse_item(item: dict) -> dict:
    """Normalize a Vinted item response into our format."""
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "brand": item.get("brand_title", "") or (item.get("brand", {}).get("title", "") if isinstance(item.get("brand"), dict) else ""),
        "price": float(item.get("price", {}).get("amount", 0) if isinstance(item.get("price"), dict) else item.get("price", 0) or 0),
        "currency": item.get("price", {}).get("currency_code", "EUR") if isinstance(item.get("price"), dict) else item.get("currency", "EUR"),
        "size": item.get("size_title", "") or (item.get("size", {}).get("title", "") if isinstance(item.get("size"), dict) else ""),
        "condition": item.get("status", ""),
        "description": item.get("description", ""),
        "url": item.get("url", ""),
        "photo": item.get("photo", {}).get("url", "") if isinstance(item.get("photo"), dict) else "",
        "photos": [p.get("url", "") for p in item.get("photos", []) if isinstance(p, dict)][:3],
        "favourite_count": item.get("favourite_count", 0),
        "view_count": item.get("view_count", 0),
        "category": item.get("catalog_id", ""),
        "brand_id": item.get("brand_id", ""),
    }

def parse_search_item(item: dict) -> dict:
    """Parse a search result item (lighter data)."""
    price_raw = item.get("price", "0")
    if isinstance(price_raw, dict):
        price = float(price_raw.get("amount", 0))
    else:
        try:
            price = float(str(price_raw).replace(",", ".").replace("€", "").strip())
        except (ValueError, TypeError):
            price = 0.0

    return {
        "title": item.get("title", ""),
        "price": price,
        "brand": item.get("brand_title", ""),
        "url": item.get("url", f"https://www.vinted.{VINTED_DOMAIN}/items/{item.get('id', '')}"),
        "photo": item.get("photo", {}).get("url", "") if isinstance(item.get("photo"), dict) else "",
        "favourite_count": item.get("favourite_count", 0),
        "view_count": item.get("view_count", 0),
        "size": item.get("size_title", ""),
    }


# ─── Brand tier logic ───
BRAND_TIERS = {
    "luxury": {"names": ["gucci", "prada", "louis vuitton", "chanel", "dior", "balenciaga", "saint laurent", "bottega veneta", "burberry", "versace", "fendi", "givenchy", "valentino", "celine", "loewe", "hermes", "moncler"], "margin": 0.45},
    "premium": {"names": ["ralph lauren", "tommy hilfiger", "calvin klein", "hugo boss", "michael kors", "coach", "kate spade", "ted baker", "all saints", "diesel", "lacoste", "fred perry", "barbour", "north face", "patagonia", "arc'teryx"], "margin": 0.38},
    "mid": {"names": ["nike", "adidas", "puma", "new balance", "converse", "vans", "reebok", "fila", "asics", "levi's", "dr. martens", "timberland", "carhartt", "dickies", "stüssy", "champion"], "margin": 0.32},
    "fast_fashion": {"names": ["zara", "h&m", "mango", "uniqlo", "pull & bear", "bershka", "stradivarius", "primark", "asos", "shein", "boohoo", "plt", "forever 21", "new look", "topshop"], "margin": 0.22},
}

def get_brand_tier(brand: str) -> tuple:
    b = brand.lower().strip()
    for tier_name, tier in BRAND_TIERS.items():
        if any(n in b or b in n for n in tier["names"]):
            return tier_name, tier["margin"]
    return "standard", 0.28

CONDITION_MULTIPLIERS = {
    "new_tags": 1.0, "new_no_tags": 0.9, "Neuf avec étiquette": 1.0,
    "Neuf sans étiquette": 0.9, "Très bon état": 0.75, "Bon état": 0.6,
    "Satisfaisant": 0.45, "Neu mit Etikett": 1.0, "Neu ohne Etikett": 0.9,
    "Sehr gut": 0.75, "Gut": 0.6, "Zufriedenstellend": 0.45,
    "New with tags": 1.0, "New without tags": 0.9, "Very good": 0.75,
    "Good": 0.6, "Satisfactory": 0.45,
}

def get_condition_multiplier(condition: str) -> float:
    return CONDITION_MULTIPLIERS.get(condition, 0.75)


# ─── Evaluation logic ───

def evaluate(item: dict, comparables: list, retail_price: float = None, condition_mult: float = 0.75) -> dict:
    brand = item.get("brand", "")
    asking_price = item.get("price", 0)
    tier_name, margin = get_brand_tier(brand)

    # Calculate market value from comparables
    comp_prices = [c["price"] for c in comparables if c.get("price", 0) > 0]
    avg_resale = round(sum(comp_prices) / len(comp_prices), 2) if comp_prices else None

    if avg_resale and avg_resale > 0:
        market_value = avg_resale
    elif retail_price and retail_price > 0:
        market_value = retail_price * condition_mult
    else:
        tier_estimates = {"luxury": 350, "premium": 120, "mid": 70, "fast_fashion": 25, "standard": 50}
        market_value = tier_estimates.get(tier_name, 50) * condition_mult

    suggested_buy_max = round(market_value * (1 - margin), 2)
    suggested_sell_min = round(market_value * 0.9, 2)
    suggested_sell_max = round(market_value * 1.15, 2)

    vinted_fee = lambda p: round(p * 0.05 + 0.70, 2)
    buy_price = asking_price if asking_price > 0 else suggested_buy_max

    profit_min = round(suggested_sell_min - buy_price - vinted_fee(suggested_sell_min), 2)
    profit_max = round(suggested_sell_max - buy_price - vinted_fee(suggested_sell_max), 2)

    if buy_price > 0:
        roi = profit_max / buy_price
        if roi > 0.4:
            verdict, verdict_color = "GREAT DEAL", "#10b981"
        elif roi > 0.2:
            verdict, verdict_color = "GOOD DEAL", "#22c55e"
        elif roi > 0.08:
            verdict, verdict_color = "OKAY", "#f59e0b"
        elif profit_max > 0:
            verdict, verdict_color = "THIN MARGIN", "#f97316"
        else:
            verdict, verdict_color = "PASS", "#e74c3c"
    else:
        verdict, verdict_color = "NO PRICE", "#6b7280"

    return {
        "tier": tier_name,
        "margin": margin,
        "market_value": round(market_value, 2),
        "suggested_buy_max": suggested_buy_max,
        "suggested_sell_min": suggested_sell_min,
        "suggested_sell_max": suggested_sell_max,
        "vinted_fee_est": vinted_fee(suggested_sell_max),
        "profit_min": profit_min,
        "profit_max": profit_max,
        "roi": round((profit_max / buy_price) * 100, 1) if buy_price > 0 else 0,
        "verdict": verdict,
        "verdict_color": verdict_color,
        "avg_resale": avg_resale,
        "data_source": "comparables" if avg_resale else ("retail" if retail_price else "estimate"),
    }


# ─── FastAPI app ───

vinted = VintedClient(domain=VINTED_DOMAIN)

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Vinted Flipper starting — domain: vinted.{VINTED_DOMAIN}")
    if ANTHROPIC_API_KEY:
        log.info("Anthropic API key configured ✓")
    else:
        log.warning("No ANTHROPIC_API_KEY — retail price lookups disabled")
    yield

app = FastAPI(title="Vinted Flipper", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class AnalyzeRequest(BaseModel):
    url: str

class ManualRequest(BaseModel):
    brand: str
    title: str
    price: float = 0
    condition: str = "very_good"
    retail_price: float = 0


@app.post("/api/analyze")
async def analyze_listing(req: AnalyzeRequest):
    """Full analysis: fetch item → find comparables → get retail price → evaluate."""
    item_id = extract_item_id(req.url)
    if not item_id:
        raise HTTPException(400, "Invalid Vinted URL — expected format: vinted.xx/items/12345-...")

    log.info(f"Analyzing item {item_id}...")

    # Step 1: Fetch item details
    raw_item = await vinted.get_item(item_id)
    if not raw_item:
        # Fallback: try to parse from URL slug
        search_terms = extract_search_terms(req.url)
        if search_terms:
            item = {
                "id": item_id, "title": search_terms.title(), "brand": "",
                "price": 0, "condition": "", "description": "", "url": req.url,
            }
            log.info(f"Item fetch failed, using URL slug: {search_terms}")
        else:
            raise HTTPException(502, "Could not fetch item from Vinted. Try again in a moment.")
    else:
        item = parse_item(raw_item)
        log.info(f"Got item: {item['brand']} - {item['title']} @ €{item['price']}")

    # Step 2: Search for comparables
    brand = item.get("brand", "")
    title = item.get("title", "")
    search_query = f"{brand} {title}".strip()[:60]

    raw_comparables = await vinted.search(search_query, per_page=20)
    # Filter out the item itself
    comparables = [
        parse_search_item(c) for c in raw_comparables
        if str(c.get("id", "")) != str(item_id)
    ][:15]
    log.info(f"Found {len(comparables)} comparable listings")

    # Step 3: Find retail price via AI
    retail_data = await find_retail_price(brand, title, item.get("description", ""))
    retail_price = retail_data.get("retail_price")
    log.info(f"Retail price: {'€' + str(retail_price) if retail_price else 'not found'}")

    # Step 4: Evaluate
    cond_mult = get_condition_multiplier(item.get("condition", ""))
    evaluation = evaluate(item, comparables, retail_price, cond_mult)

    return {
        "item": item,
        "comparables": comparables[:10],
        "retail": retail_data,
        "evaluation": evaluation,
    }


@app.post("/api/evaluate")
async def manual_evaluate(req: ManualRequest):
    """Manual evaluation without fetching from Vinted."""
    item = {"brand": req.brand, "title": req.title, "price": req.price, "condition": req.condition}
    cond_mult = get_condition_multiplier(req.condition)

    # Search for comparables
    search_query = f"{req.brand} {req.title}".strip()[:60]
    raw_comparables = await vinted.search(search_query, per_page=15)
    comparables = [parse_search_item(c) for c in raw_comparables][:10]

    retail_price = req.retail_price if req.retail_price > 0 else None
    if not retail_price and ANTHROPIC_API_KEY:
        retail_data = await find_retail_price(req.brand, req.title)
        retail_price = retail_data.get("retail_price")
    else:
        retail_data = {"retail_price": retail_price, "source": "manual", "notes": ""}

    evaluation = evaluate(item, comparables, retail_price, cond_mult)

    return {
        "item": item,
        "comparables": comparables,
        "retail": retail_data,
        "evaluation": evaluation,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "domain": f"vinted.{VINTED_DOMAIN}",
        "ai_enabled": bool(ANTHROPIC_API_KEY),
    }


# Serve frontend
frontend_dir = Path(__file__).parent / "frontend"
if frontend_dir.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dir / "assets" if (frontend_dir / "assets").exists() else frontend_dir), name="assets")

    @app.get("/{path:path}")
    async def serve_frontend(path: str):
        file_path = frontend_dir / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(frontend_dir / "index.html")
