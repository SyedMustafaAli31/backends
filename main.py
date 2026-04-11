from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import httpx
import asyncio
import json
import os
import re
import trafilatura
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

app = FastAPI(title="Social Media Research API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request/Response Models ────────────────────────────────
class ResearchRequest(BaseModel):
    title: str
    goal: str
    niche: Optional[str] = ""
    platforms: List[str]
    serpapi_key: str
    max_searches: int = 10
    queries: Optional[List[str]] = None   # AI-generated queries from Flutter

class ScrapeRequest(BaseModel):
    url: str

class ResearchResponse(BaseModel):
    success: bool
    data: str
    sources: List[str] = []
    queries_used: List[str] = []
    error: Optional[str] = None


# ══════════════════════════════════════════════════════════
#  Fallback Query Builder (used only when Flutter doesn't
#  send AI-generated queries)
# ══════════════════════════════════════════════════════════
def build_fallback_queries(
    title: str, goal: str, niche: str, platforms: List[str], max_queries: int
) -> List[str]:
    """
    Generic fallback queries — used only when AI-generated queries
    are NOT provided from the Flutter app.
    Covers diverse research angles automatically.
    """
    niche_or_title = niche if niche else title
    platform_primary = platforms[0] if platforms else "social media"

    all_queries = [
        f"{niche_or_title} trending content ideas 2026",
        f"{goal} best performing posts {platform_primary} 2026",
        f"{niche_or_title} viral content strategy social media",
        f"{title} competitor analysis social media 2025",
        f"{niche_or_title} audience pain points and desires",
        f"top {niche_or_title} influencer content strategy 2026",
        f"{niche_or_title} latest statistics and data 2025 2026",
        f"{niche_or_title} {platform_primary} content ideas that went viral",
        f"best {goal} campaign examples social media",
        f"{niche_or_title} trending hashtags {platform_primary} 2026",
    ]

    return all_queries[:max_queries]


# ══════════════════════════════════════════════════════════
#  GOOGLE TRENDS — interest over time + rising queries
# ══════════════════════════════════════════════════════════
async def fetch_google_trends(
    serpapi_key: str,
    keyword: str,
    geo: str = "IN",
) -> str:
    """
    SerpAPI ke through Google Trends data fetch karta hai.
    - Interest over time (last 3 months)
    - Rising related queries (HOT right now)
    - Top related queries
    """
    if not keyword or not serpapi_key:
        return ""

    buffer = []
    # Use first keyword only (comma-sep not needed)
    kw = keyword.split(",")[0].strip()[:100]

    async with httpx.AsyncClient(timeout=20) as client:
        # ── 1. Interest over time ─────────────────────────
        try:
            resp = await client.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google_trends",
                    "q": kw,
                    "api_key": serpapi_key,
                    "data_type": "TIMESERIES",
                    "date": "today 3-m",
                    "geo": geo,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                timeline = data.get("interest_over_time", {}).get("timeline_data", [])
                if timeline:
                    values = []
                    for point in timeline:
                        for val in point.get("values", []):
                            v = val.get("value")
                            if v is not None:
                                try:
                                    values.append(int(v))
                                except (ValueError, TypeError):
                                    pass
                    if values:
                        avg = sum(values) / len(values)
                        last = values[-1]
                        direction = "Rising ↑" if last >= avg else "Declining ↓"
                        buffer.append(f"GOOGLE TRENDS — '{kw}'")
                        buffer.append(f"  Trend direction  : {direction}")
                        buffer.append(f"  Interest (0-100) : peak={max(values)}, recent={last}, avg={int(avg)}")
        except Exception as e:
            print(f"[Trends TIMESERIES] {e}")

        # ── 2. Rising + Top related queries ──────────────
        try:
            resp = await client.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google_trends",
                    "q": kw,
                    "api_key": serpapi_key,
                    "data_type": "RELATED_QUERIES",
                    "date": "today 3-m",
                    "geo": geo,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                related = data.get("related_queries", {})
                rising = related.get("rising", [])
                top    = related.get("top", [])

                if rising:
                    buffer.append("  Rising queries (🔥 trending NOW):")
                    for q in rising[:8]:
                        query = q.get("query", "")
                        value = q.get("value", "")
                        if query:
                            buffer.append(f"    🔥 {query}  [{value}]")

                if top:
                    buffer.append("  Top searched queries:")
                    for q in top[:6]:
                        query = q.get("query", "")
                        value = q.get("value", "")
                        if query:
                            buffer.append(f"    → {query}  [{value}]")
        except Exception as e:
            print(f"[Trends RELATED_QUERIES] {e}")

        # ── 3. Related topics ──────────────────────────────
        try:
            resp = await client.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google_trends",
                    "q": kw,
                    "api_key": serpapi_key,
                    "data_type": "RELATED_TOPICS",
                    "date": "today 3-m",
                    "geo": geo,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                topics = data.get("related_topics", {})
                rising_topics = topics.get("rising", [])
                if rising_topics:
                    buffer.append("  Rising topics (breakout areas):")
                    for t in rising_topics[:5]:
                        title_t = t.get("topic", {}).get("title", "")
                        value   = t.get("value", "")
                        if title_t:
                            buffer.append(f"    ✦ {title_t}  [{value}]")
        except Exception as e:
            print(f"[Trends RELATED_TOPICS] {e}")

    return "\n".join(buffer)


# ══════════════════════════════════════════════════════════
#  STEP 1: SerpAPI — real Google search results
# ══════════════════════════════════════════════════════════
async def fetch_serp_results(
    serpapi_key: str,
    queries: List[str],
) -> tuple[str, list[str]]:
    """
    Provided queries ke liye SerpAPI se real Google results fetch karta hai.
    Queries ya toh Flutter AI-generated hain ya fallback builder se.
    """
    buffer = []
    all_urls = []

    async with httpx.AsyncClient(timeout=20) as client:
        for query in queries:
            try:
                resp = await client.get(
                    "https://serpapi.com/search",
                    params={
                        "q": query,
                        "api_key": serpapi_key,
                        "num": "10",
                        "gl": "in",
                        "hl": "en",
                    }
                )
                if resp.status_code != 200:
                    continue

                data = resp.json()
                organic = data.get("organic_results", [])
                if not organic:
                    continue

                buffer.append(f'=== QUERY: "{query}" ===')

                for r in organic[:8]:
                    title_r = r.get("title", "")
                    snippet = r.get("snippet", "")
                    link = r.get("link", "")

                    if title_r:
                        buffer.append(f"TITLE: {title_r}")
                        if snippet:
                            buffer.append(f"SNIPPET: {snippet}")
                        if link:
                            buffer.append(f"URL: {link}")
                            all_urls.append(link)
                        buffer.append("")

                # People Also Ask — real audience questions
                paa = data.get("related_questions", [])
                if paa:
                    buffer.append("PEOPLE ALSO ASK:")
                    for q in paa[:5]:
                        question = q.get("question", "")
                        answer = q.get("snippet", "")
                        if question:
                            buffer.append(f"  Q: {question}")
                            if answer:
                                buffer.append(f"  A: {answer[:200]}")
                    buffer.append("")

                # Related searches — discover more angles
                related = data.get("related_searches", [])
                if related:
                    buffer.append("RELATED SEARCHES:")
                    for s in related[:6]:
                        q2 = s.get("query", "")
                        if q2:
                            buffer.append(f"  - {q2}")
                    buffer.append("")

                # Knowledge graph if available
                kg = data.get("knowledge_graph", {})
                if kg:
                    kg_desc = kg.get("description", "")
                    if kg_desc:
                        buffer.append(f"KNOWLEDGE GRAPH: {kg_desc[:300]}")
                        buffer.append("")

            except Exception as e:
                print(f"SerpAPI error for '{query}': {e}")
                continue

    return "\n".join(buffer), all_urls


# ══════════════════════════════════════════════════════════
#  STEP 2a: Playwright — open JS-heavy sites
# ══════════════════════════════════════════════════════════
async def scrape_with_playwright(url: str) -> str:
    """Use Playwright to render JS sites and get full HTML"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        print(f"Playwright error for {url}: {e}")
        return ""


# ══════════════════════════════════════════════════════════
#  STEP 2b: Trafilatura — extract clean article text
# ══════════════════════════════════════════════════════════
def extract_clean_text(html: str, url: str = "") -> str:
    if not html:
        return ""

    result = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        favor_precision=False,
    )

    if result and len(result) > 100:
        return result[:3000]  # richer context per source

    return extract_with_bs4(html)


def extract_with_bs4(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "advertisement", "iframe", "form"]):
            tag.decompose()

        main_content = (
            soup.find("article") or
            soup.find("main") or
            soup.find(class_=re.compile(r"content|article|post|entry", re.I)) or
            soup.find("body")
        )

        if not main_content:
            return ""

        paragraphs = []
        for p in main_content.find_all(["p", "h1", "h2", "h3", "li"]):
            text = p.get_text(strip=True)
            if text and len(text) > 20:
                paragraphs.append(text)

        result = " ".join(paragraphs)
        result = re.sub(r'\s+', ' ', result).strip()
        return result[:3000]

    except Exception as e:
        print(f"BeautifulSoup error: {e}")
        return ""


# ══════════════════════════════════════════════════════════
#  STEP 3: Fetch top URLs with httpx + Playwright fallback
# ══════════════════════════════════════════════════════════

SKIP_DOMAINS = {
    "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "linkedin.com", "facebook.com", "youtube.com",
    "pinterest.com", "reddit.com", "quora.com",
}

async def fetch_and_extract_urls(urls: list[str], max_urls: int = 10) -> tuple[str, list[str]]:
    """Fetch top URLs using httpx + Playwright fallback — deduplicated by domain"""
    buffer = []
    used_sources = []

    filtered = []
    seen_domains = set()
    for url in urls:
        try:
            domain = url.split("/")[2].replace("www.", "")
            if domain in SKIP_DOMAINS:
                continue
            if domain in seen_domains:
                continue
            if url.endswith(".pdf") or url.endswith(".xml"):
                continue
            seen_domains.add(domain)
            filtered.append(url)
        except:
            continue

    tasks = [_fetch_one_url(url) for url in filtered[:max_urls]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for url, result in zip(filtered[:max_urls], results):
        if isinstance(result, Exception) or not result:
            continue
        buffer.append(f"--- Source: {url} ---")
        buffer.append(result)
        buffer.append("")
        used_sources.append(url)

    return "\n".join(buffer), used_sources


# Browser-like headers
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_PLAYWRIGHT_ENABLED = os.environ.get("ENABLE_PLAYWRIGHT", "false").lower() == "true"


async def _fetch_one_url(url: str) -> str:
    """
    Free plan:  httpx with browser-like headers (covers 90%+ sites)
    Paid plan:  httpx first, Playwright fallback for JS-heavy sites
                Set ENABLE_PLAYWRIGHT=true in Railway variables
    """
    html = ""

    # Try 1: httpx with real browser headers
    try:
        async with httpx.AsyncClient(
            timeout=12,
            headers=_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                html = resp.text
    except Exception:
        pass

    # Try 2: httpx with Googlebot UA (some sites block Chrome UA)
    if not html or len(html) < 500:
        try:
            async with httpx.AsyncClient(
                timeout=12,
                headers={**_HEADERS, "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    html = resp.text
        except Exception:
            pass

    # Try 3: Playwright — only on paid plan (ENABLE_PLAYWRIGHT=true)
    if _PLAYWRIGHT_ENABLED and (not html or len(html) < 500):
        html = await scrape_with_playwright(url)

    if not html:
        return ""

    return extract_clean_text(html, url)


# ══════════════════════════════════════════════════════════
#  Research Summary Builder
# ══════════════════════════════════════════════════════════
def build_research_summary(
    title: str, goal: str, niche: str, platforms: List[str],
    queries_used: List[str], serp_data: str,
    url_content: str, sources: list[str],
    trends_data: str = "",
) -> str:
    """
    Saara scraped data ko structured LLM-ready format mein organize karta hai.
    """
    platform_str = ", ".join(platforms)
    lines = []
    lines.append("╔══════════════════════════════════════════════════════╗")
    lines.append("║         REAL-TIME RESEARCH CONTEXT FOR AI           ║")
    lines.append("╚══════════════════════════════════════════════════════╝")
    lines.append("")
    lines.append(f"Campaign : {title}")
    lines.append(f"Goal     : {goal}")
    if niche:
        lines.append(f"Niche    : {niche}")
    lines.append(f"Platforms: {platform_str}")
    lines.append(f"Queries  : {len(queries_used)} searches performed")
    lines.append("")

    if trends_data:
        lines.append("─── GOOGLE TRENDS (LIVE) ───")
        lines.append(trends_data)
        lines.append("")

    if queries_used:
        lines.append("─── QUERIES SEARCHED ───")
        for i, q in enumerate(queries_used, 1):
            lines.append(f"  {i}. {q}")
        lines.append("")

    lines.append("─── LIVE GOOGLE SEARCH DATA ───")
    lines.append(serp_data)

    if url_content:
        lines.append("")
        lines.append("─── SCRAPED ARTICLE CONTENT (TOP SOURCES) ───")
        lines.append(url_content)

    if sources:
        lines.append("")
        lines.append("─── SOURCES SCRAPED ───")
        for i, src in enumerate(sources, 1):
            lines.append(f"  {i}. {src}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("INSTRUCTION TO AI: The above is LIVE data from Google.")
    lines.append("1. Use GOOGLE TRENDS rising queries as post hooks and hashtag ideas.")
    lines.append("2. Use RISING TOPICS to angle posts toward what audiences are searching NOW.")
    lines.append("3. Use real titles, snippets, and statistics from search results.")
    lines.append("4. Do NOT fall back on generic knowledge — use this real-time data.")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "3.0.0",
        "playwright": _PLAYWRIGHT_ENABLED,
        "plan": "paid" if _PLAYWRIGHT_ENABLED else "free",
    }


@app.post("/research", response_model=ResearchResponse)
async def research(req: ResearchRequest):
    """
    Full research pipeline:
    1. Queries: Flutter AI-generated queries use karo (agar hain), warna fallback builder
    2. SerpAPI: Har query pe real Google search
    3. Scraping: Top URLs se clean article text (httpx + optional Playwright)
    4. Summary: Structured LLM-ready research context build karo
    """
    if not req.serpapi_key:
        raise HTTPException(status_code=400, detail="serpapi_key is required")

    niche = req.niche or ""

    try:
        # Step 1: Queries decide karo
        # Flutter ne AI-generated queries bheje hain → woh use karo
        # Nahi bheje → fallback queries generate karo
        if req.queries and len(req.queries) > 0:
            queries_to_use = req.queries[:req.max_searches]
            print(f"[Research] Using {len(queries_to_use)} AI-generated queries from Flutter")
        else:
            queries_to_use = build_fallback_queries(
                title=req.title,
                goal=req.goal,
                niche=niche,
                platforms=req.platforms,
                max_queries=req.max_searches,
            )
            print(f"[Research] Using {len(queries_to_use)} fallback-generated queries")

        # Step 2: SerpAPI + Google Trends parallel fetch
        trends_keyword = niche if niche else req.title
        serp_task   = fetch_serp_results(serpapi_key=req.serpapi_key, queries=queries_to_use)
        trends_task = fetch_google_trends(serpapi_key=req.serpapi_key, keyword=trends_keyword)

        (serp_data, urls), trends_data = await asyncio.gather(serp_task, trends_task)

        if not serp_data:
            return ResearchResponse(
                success=False,
                data="",
                queries_used=queries_to_use,
                error="SerpAPI returned no results. Check your API key."
            )

        # Step 3: Top 10 URLs scrape karo
        max_urls = min(10, max(6, len(queries_to_use)))
        url_content, sources = await fetch_and_extract_urls(urls, max_urls=max_urls)

        # Step 4: Structured research summary build karo
        final_data = build_research_summary(
            title=req.title,
            goal=req.goal,
            niche=niche,
            platforms=req.platforms,
            queries_used=queries_to_use,
            serp_data=serp_data,
            url_content=url_content,
            sources=sources,
            trends_data=trends_data,
        )

        return ResearchResponse(
            success=True,
            data=final_data,
            sources=sources,
            queries_used=queries_to_use,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
#  KEYWORD SUGGESTIONS — LLM se title ke basis pe keywords
# ══════════════════════════════════════════════════════════════
class KeywordRequest(BaseModel):
    title: str
    niche: Optional[str] = ""
    location: Optional[str] = ""
    ai_api_key: str
    ai_provider: str = "gemini"  # gemini / groq / openai

@app.post("/keyword-suggestions")
async def keyword_suggestions(req: KeywordRequest):
    """Campaign title ke basis pe 12-15 relevant keywords suggest karta hai."""
    prompt = f"""You are a local marketing expert. A business owner wants to create a social media campaign.

Campaign Title: "{req.title}"
Business Niche: "{req.niche or 'General Business'}"
Location: "{req.location or 'India'}"

Generate 12-15 highly relevant keywords for competitor research and local SEO.
These keywords will be used to find local competitors and trending content.

Rules:
- Include location-specific terms if location is provided
- Mix broad + niche + local keywords
- Include both English and common local language terms if applicable
- Focus on what customers would search for
- Include competitor-finding keywords (e.g., "best biryani near me")

Return ONLY a JSON array of keyword strings, no explanation:
["keyword1", "keyword2", ...]"""

    headers = {"Content-Type": "application/json"}
    try:
        raw = ""
        if req.ai_provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={req.ai_api_key}"
            resp = await _call_ai_api(url, prompt, headers={})
            raw = resp
        elif req.ai_provider == "groq":
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {req.ai_api_key}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 500}
                )
                raw = r.json()["choices"][0]["message"]["content"]
        elif req.ai_provider == "openai":
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {req.ai_api_key}", "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 500}
                )
                raw = r.json()["choices"][0]["message"]["content"]

        # Parse JSON array from response
        cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
        start = cleaned.find("[")
        end = cleaned.rfind("]") + 1
        if start >= 0 and end > start:
            keywords = json.loads(cleaned[start:end])
            return {"success": True, "keywords": keywords[:15]}
        return {"success": False, "keywords": [], "error": "Could not parse keywords"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _call_ai_api(url: str, prompt: str, headers: dict) -> str:
    """Call Gemini API and return text response."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 1000, "temperature": 0.7}
        })
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


# ══════════════════════════════════════════════════════════════
#  COMPETITOR DISCOVERY — Location + Keywords + SerpAPI
# ══════════════════════════════════════════════════════════════
class CompetitorRequest(BaseModel):
    location: str           # "Banjara Hills, Hyderabad"
    keywords: List[str]     # max 5 selected by user
    radius_km: float = 10   # 2 / 5 / 10 / custom / 0 = worldwide
    serpapi_key: str
    ai_api_key: str
    ai_provider: str = "gemini"
    max_competitors: int = 8

class CompetitorInfo(BaseModel):
    name: str
    address: Optional[str] = ""
    distance: Optional[str] = ""
    rating: Optional[float] = None
    reviews: Optional[int] = None
    website: Optional[str] = ""
    phone: Optional[str] = ""
    place_id: Optional[str] = ""
    thumbnail: Optional[str] = ""
    gps_coords: Optional[dict] = None
    social_links: Optional[dict] = None
    scraped_content: Optional[str] = ""
    ai_analysis: Optional[dict] = None

@app.post("/competitors")
async def find_competitors(req: CompetitorRequest):
    """
    Location + keywords se local competitors dhundhta hai.
    Uses SerpAPI Google Maps + website scraping + AI analysis.
    """
    competitors = []
    seen_names = set()

    # Build search queries from keywords
    primary_kw = " ".join(req.keywords[:3])
    queries = [
        f"{primary_kw} near {req.location}",
        f"best {primary_kw} in {req.location}",
        f"{req.keywords[0]} {req.location}" if req.keywords else req.location,
    ]
    if req.radius_km == 0:  # worldwide
        queries = [f"top {primary_kw} businesses", f"best {primary_kw} worldwide"]

    # Search via SerpAPI Google Maps
    async with httpx.AsyncClient(timeout=30) as client:
        for query in queries[:2]:
            if len(competitors) >= req.max_competitors:
                break
            try:
                params = {
                    "engine": "google_maps",
                    "q": query,
                    "api_key": req.serpapi_key,
                    "type": "search",
                    "num": "10",
                }
                if req.radius_km > 0:
                    # Add radius hint in query
                    params["q"] = f"{query} within {int(req.radius_km)}km"

                r = await client.get("https://serpapi.com/search", params=params)
                data = r.json()

                places = data.get("local_results", []) or data.get("places", [])
                for place in places:
                    name = place.get("title", "") or place.get("name", "")
                    if not name or name.lower() in seen_names:
                        continue
                    seen_names.add(name.lower())

                    dist_raw = place.get("distance", "")
                    website = place.get("website", "") or place.get("links", {}).get("website", "")

                    comp = {
                        "name": name,
                        "address": place.get("address", ""),
                        "distance": dist_raw,
                        "rating": place.get("rating"),
                        "reviews": place.get("reviews", 0),
                        "website": website,
                        "phone": place.get("phone", ""),
                        "place_id": place.get("place_id", ""),
                        "thumbnail": place.get("thumbnail", "") or place.get("photos", [{}])[0].get("thumbnail", "") if place.get("photos") else "",
                        "gps_coords": place.get("gps_coordinates"),
                        "social_links": {},
                        "scraped_content": "",
                        "ai_analysis": None,
                    }

                    # Filter by radius (if SerpAPI gives distance)
                    if req.radius_km > 0 and dist_raw:
                        try:
                            dist_val = float(re.sub(r"[^\d.]", "", dist_raw.split()[0]))
                            is_miles = "mi" in dist_raw.lower()
                            dist_km = dist_val * 1.609 if is_miles else dist_val
                            if dist_km > req.radius_km * 1.5:  # 1.5x buffer
                                continue
                        except Exception:
                            pass

                    competitors.append(comp)
                    if len(competitors) >= req.max_competitors:
                        break

            except Exception as e:
                continue

    # Also search SerpAPI organic for competitor websites
    if len(competitors) < 3:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get("https://serpapi.com/search", params={
                    "engine": "google",
                    "q": f"top {primary_kw} {req.location} competitors",
                    "api_key": req.serpapi_key,
                    "num": "5",
                })
                data = r.json()
                for result in data.get("organic_results", [])[:5]:
                    name = result.get("title", "").split(" - ")[0].split("|")[0].strip()
                    if name and name.lower() not in seen_names and len(name) < 60:
                        seen_names.add(name.lower())
                        competitors.append({
                            "name": name,
                            "address": "",
                            "distance": "",
                            "rating": None,
                            "reviews": 0,
                            "website": result.get("link", ""),
                            "phone": "",
                            "place_id": "",
                            "thumbnail": result.get("thumbnail", ""),
                            "gps_coords": None,
                            "social_links": {},
                            "scraped_content": "",
                            "ai_analysis": None,
                        })
        except Exception:
            pass

    # Scrape websites + run AI analysis in parallel
    scrape_tasks = []
    for comp in competitors[:req.max_competitors]:
        if comp.get("website"):
            scrape_tasks.append(_enrich_competitor(comp, req))
        else:
            scrape_tasks.append(_analyze_competitor_no_web(comp, req))

    enriched = await asyncio.gather(*scrape_tasks, return_exceptions=True)
    final = []
    for i, result in enumerate(enriched):
        if isinstance(result, Exception):
            final.append(competitors[i] if i < len(competitors) else {})
        else:
            final.append(result)

    return {
        "success": True,
        "location": req.location,
        "radius_km": req.radius_km,
        "total": len(final),
        "competitors": final,
    }


async def _enrich_competitor(comp: dict, req: CompetitorRequest) -> dict:
    """Scrape website + social links + run AI analysis."""
    website = comp.get("website", "")
    scraped = ""
    social_links = {}

    if website:
        try:
            content = await _fetch_one_url(website)
            if content:
                scraped = content[:3000]
                # Try to find social links
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(website, headers={"User-Agent": "Mozilla/5.0"}, timeout=8, follow_redirects=True)
                    html = r.text
                    for pattern, key in [
                        (r'instagram\.com/([A-Za-z0-9_.]+)', 'instagram'),
                        (r'facebook\.com/([A-Za-z0-9_.]+)', 'facebook'),
                        (r'twitter\.com/([A-Za-z0-9_]+)', 'twitter'),
                        (r'youtube\.com/(@?[A-Za-z0-9_-]+)', 'youtube'),
                    ]:
                        m = re.search(pattern, html)
                        if m:
                            social_links[key] = f"https://{key}.com/{m.group(1)}"
        except Exception:
            pass

    comp["scraped_content"] = scraped
    comp["social_links"] = social_links
    comp["ai_analysis"] = await _ai_analyze_competitor(comp, req)
    return comp


async def _analyze_competitor_no_web(comp: dict, req: CompetitorRequest) -> dict:
    """AI analysis without website — based on name, rating, location."""
    comp["ai_analysis"] = await _ai_analyze_competitor(comp, req)
    return comp


async def _ai_analyze_competitor(comp: dict, req: CompetitorRequest) -> dict:
    """Generate AI analysis for a competitor."""
    scraped = comp.get("scraped_content", "")
    website_section = f"\nWebsite Content (first 2000 chars):\n{scraped[:2000]}" if scraped else ""

    prompt = f"""You are an expert social media marketing analyst. Analyze this competitor for a business campaign.

COMPETITOR INFO:
Name: {comp.get('name', 'Unknown')}
Location: {comp.get('address', req.location)}
Rating: {comp.get('rating', 'N/A')} ({comp.get('reviews', 0)} reviews)
Keywords/Niche: {', '.join(req.keywords)}{website_section}

Generate a detailed competitor analysis. Return ONLY this JSON:
{{
  "strength_score": <0-100 integer>,
  "threat_level": "High" | "Medium" | "Low",
  "best_content": ["insight 1", "insight 2", "insight 3"],
  "hook_style": ["hook style 1", "hook style 2"],
  "posting_pattern": "description of likely posting frequency and timing",
  "offer_types": ["offer 1", "offer 2"],
  "visual_style": "description of their likely visual branding",
  "weaknesses": ["weakness 1", "weakness 2", "weakness 3"],
  "opportunities": ["opportunity 1", "opportunity 2", "opportunity 3"],
  "summary": "one line summary of this competitor"
}}"""

    try:
        raw = ""
        if req.ai_provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={req.ai_api_key}"
            raw = await _call_ai_api(url, prompt, {})
        elif req.ai_provider == "groq":
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {req.ai_api_key}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 800}
                )
                raw = r.json()["choices"][0]["message"]["content"]
        elif req.ai_provider == "openai":
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {req.ai_api_key}", "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 800}
                )
                raw = r.json()["choices"][0]["message"]["content"]

        cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end])
    except Exception:
        pass

    # Fallback
    return {
        "strength_score": 50,
        "threat_level": "Medium",
        "best_content": ["Social media presence", "Local community engagement"],
        "hook_style": ["Local relevance", "Customer reviews"],
        "posting_pattern": "Regular posting, 3-5 times per week",
        "offer_types": ["Standard offers", "Seasonal promotions"],
        "visual_style": "Professional branding with local appeal",
        "weaknesses": ["Limited online presence data available"],
        "opportunities": ["Engage more on social media", "Target younger demographics"],
        "summary": f"Local competitor in {req.location} space",
    }


@app.post("/scrape")
async def scrape_url(req: ScrapeRequest):
    """
    Single URL ko scrape karke clean text return karta hai.
    Free plan: httpx + Googlebot UA fallback
    Paid plan: Playwright also used (ENABLE_PLAYWRIGHT=true)
    """
    content = await _fetch_one_url(req.url)
    if not content:
        raise HTTPException(status_code=422, detail="Could not extract content from URL")
    return {"success": True, "content": content, "url": req.url}


# ══════════════════════════════════════════════════════════════
#  VIDEO GENERATION — HuggingFace SDK → Replicate → Wan2.2-T2V-A14B
#
#  HF token comes from Flutter (admin sets it in Admin Panel →
#  saved to Firestore system_config/video_config → Flutter reads
#  it and passes as hf_token in request body).
#
#  Exact same as working Colab:
#    from huggingface_hub import InferenceClient
#    client = InferenceClient(provider="replicate", api_key=hf_token)
#    video = client.text_to_video("...", model="Wan-AI/Wan2.2-T2V-A14B")
# ══════════════════════════════════════════════════════════════
class VideoGenerateRequest(BaseModel):
    prompt: str
    hf_token: str  # from Admin Panel → Firestore → Flutter

@app.post("/generate-video")
async def generate_video(req: VideoGenerateRequest):
    import base64
    from concurrent.futures import ThreadPoolExecutor
    from huggingface_hub import InferenceClient

    # Fallback to Railway env var if Flutter didn't send a token
    hf_token = req.hf_token or os.environ.get("HF_TOKEN", "")
    if not hf_token:
        raise HTTPException(status_code=500, detail="HF token missing. Admin Panel → API Keys → AI Video Creator → HuggingFace Token set karo.")

    full_prompt = (
        f"{req.prompt}. "
        "Cinematic, dramatic lighting, smooth camera motion, Instagram reel style, 5 seconds."
    )

    def _run_sync():
        client = InferenceClient(
            provider="replicate",
            api_key=hf_token,
        )
        video_bytes = client.text_to_video(
            full_prompt,
            model="Wan-AI/Wan2.2-T2V-A14B",
        )
        return video_bytes

    try:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            video_bytes = await asyncio.wait_for(
                loop.run_in_executor(pool, _run_sync),
                timeout=300,
            )

        if not video_bytes:
            raise HTTPException(status_code=500, detail="Video generation returned empty bytes")

        return {
            "success": True,
            "video_b64": base64.b64encode(video_bytes).decode(),
            "content_type": "video/mp4",
        }

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Video generation timed out (5 min limit)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Video Gen Failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
