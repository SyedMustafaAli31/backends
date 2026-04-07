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
#  VIDEO GENERATION — HuggingFace → Replicate → Wan2.2
#  Mirrors: InferenceClient(provider="replicate").text_to_video()
# ══════════════════════════════════════════════════════════════
class VideoGenerateRequest(BaseModel):
    prompt: str
    hf_token: str

@app.post("/generate-video")
async def generate_video(req: VideoGenerateRequest):
    """
    Generates a short video clip via HuggingFace router → Replicate → Wan2.2-T2V-A14B.
    Returns base64-encoded mp4 video bytes.
    """
    model = "Wan-AI/Wan2.2-T2V-A14B"
    full_prompt = f"{req.prompt}. Cinematic, dramatic lighting, smooth camera motion, Instagram reel style, 5 seconds."

    headers = {
        "Authorization": f"Bearer {req.hf_token}",
        "Content-Type": "application/json",
        "x-provider": "replicate",
    }

    async with httpx.AsyncClient(timeout=300) as client:
        # Submit the generation job
        resp = await client.post(
            f"https://router.huggingface.co/models/{model}",
            headers=headers,
            json={"inputs": full_prompt},
        )

        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            if "video" in ct:
                import base64
                return {
                    "success": True,
                    "video_b64": base64.b64encode(resp.content).decode(),
                    "content_type": "video/mp4",
                }
            # JSON response — may have url or request_id
            try:
                data = resp.json()
                if isinstance(data, dict):
                    if data.get("url"):
                        return {"success": True, "video_url": data["url"]}
                    if data.get("video"):
                        return {"success": True, "video_url": data["video"]}
                    req_id = data.get("request_id") or data.get("id")
                    if req_id:
                        return await _poll_video_result(client, req_id, model, req.hf_token)
            except Exception:
                pass

        if resp.status_code in (201, 202):
            try:
                data = resp.json()
                req_id = data.get("request_id") or data.get("id")
                if req_id:
                    return await _poll_video_result(client, req_id, model, req.hf_token)
            except Exception:
                pass

        raise HTTPException(
            status_code=resp.status_code,
            detail=f"HF API Error: {resp.text[:500]}"
        )


async def _poll_video_result(client: httpx.AsyncClient, request_id: str, model: str, hf_token: str):
    """Poll until Replicate job completes and return video URL or b64."""
    import base64
    poll_url = f"https://router.huggingface.co/models/{model}/status/{request_id}"
    headers = {
        "Authorization": f"Bearer {hf_token}",
        "x-provider": "replicate",
    }

    for _ in range(40):  # ~4 min max
        await asyncio.sleep(6)
        try:
            res = await client.get(poll_url, headers=headers, timeout=30)
            if res.status_code == 200:
                ct = res.headers.get("content-type", "")
                if "video" in ct:
                    return {
                        "success": True,
                        "video_b64": base64.b64encode(res.content).decode(),
                        "content_type": "video/mp4",
                    }
                try:
                    data = res.json()
                    status = data.get("status", "")
                    if status in ("COMPLETED", "succeeded"):
                        url = (data.get("url") or
                               (data.get("output") or {}).get("url") or
                               data.get("video"))
                        if url:
                            return {"success": True, "video_url": url}
                    if data.get("url"):
                        return {"success": True, "video_url": data["url"]}
                    if status in ("failed", "error"):
                        raise HTTPException(status_code=500, detail="Video generation failed on provider")
                except httpx.DecodingError:
                    pass
        except httpx.TimeoutException:
            continue

    raise HTTPException(status_code=504, detail="Video generation timed out after 4 minutes")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
