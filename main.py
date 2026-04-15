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
    url_content: str, sources: list[str]
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
    lines.append("Use real titles, snippets, statistics, and insights from")
    lines.append("this research. Do NOT fall back on generic knowledge.")
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

        # Step 2: SerpAPI se real Google results
        serp_data, urls = await fetch_serp_results(
            serpapi_key=req.serpapi_key,
            queries=queries_to_use,
        )

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


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
