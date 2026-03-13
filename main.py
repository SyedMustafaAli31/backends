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

app = FastAPI(title="Social Media Research API", version="1.0.0")

# Allow Flutter app to call this API
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
    platforms: List[str]
    serpapi_key: str
    max_searches: int = 1

class ScrapeRequest(BaseModel):
    url: str

class ResearchResponse(BaseModel):
    success: bool
    data: str
    sources: List[str] = []
    error: Optional[str] = None


# ══════════════════════════════════════════════════════════
#  STEP 1: SerpAPI — real Google search results
# ══════════════════════════════════════════════════════════
async def fetch_serp_results(
    serpapi_key: str,
    title: str,
    goal: str,
    platforms: str,
    max_queries: int = 1
) -> tuple[str, list[str]]:
    """Fetch real Google results via SerpAPI"""

    queries = [
        f"{title} trending {goal}",
        f"{title} viral posts {platforms}" if max_queries > 1 else None,
        f"{title} competitor content strategy" if max_queries > 2 else None,
    ]
    queries = [q for q in queries if q][:max_queries]

    buffer = []
    all_urls = []

    async with httpx.AsyncClient(timeout=15) as client:
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

                buffer.append(f'=== GOOGLE: "{query}" ===')

                for r in organic[:6]:
                    title_r  = r.get("title", "")
                    snippet  = r.get("snippet", "")
                    link     = r.get("link", "")

                    if title_r:
                        buffer.append(f"TITLE: {title_r}")
                        if snippet:
                            buffer.append(f"SNIPPET: {snippet}")
                        if link:
                            buffer.append(f"URL: {link}")
                            all_urls.append(link)
                        buffer.append("")

                # People Also Ask
                paa = data.get("related_questions", [])
                if paa:
                    buffer.append("PEOPLE ALSO ASK:")
                    for q in paa[:5]:
                        question = q.get("question", "")
                        if question:
                            buffer.append(f"  - {question}")
                    buffer.append("")

                # Related searches
                related = data.get("related_searches", [])
                if related:
                    buffer.append("RELATED SEARCHES:")
                    for s in related[:6]:
                        q2 = s.get("query", "")
                        if q2:
                            buffer.append(f"  - {q2}")
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
            await page.wait_for_timeout(2000)  # wait for JS to render
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
    """
    Trafilatura: removes ads, nav, footer, scripts
    Extracts main article content only
    """
    if not html:
        return ""

    # Try Trafilatura first (best quality)
    result = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        favor_precision=False,
    )

    if result and len(result) > 100:
        return result[:1500]  # cap at 1500 chars per source

    # Fallback: BeautifulSoup manual extraction
    return extract_with_bs4(html)


def extract_with_bs4(html: str) -> str:
    """
    BeautifulSoup fallback:
    Removes noise, extracts main text
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "advertisement", "iframe", "form",
                         "[class*='ad']", "[class*='banner']", "[class*='cookie']"]):
            tag.decompose()

        # Try to find main content
        main_content = (
            soup.find("article") or
            soup.find("main") or
            soup.find(class_=re.compile(r"content|article|post|entry", re.I)) or
            soup.find("body")
        )

        if not main_content:
            return ""

        # Extract text with paragraph breaks
        paragraphs = []
        for p in main_content.find_all(["p", "h1", "h2", "h3", "li"]):
            text = p.get_text(strip=True)
            if text and len(text) > 20:
                paragraphs.append(text)

        result = " ".join(paragraphs)

        # Collapse whitespace
        result = re.sub(r'\s+', ' ', result).strip()
        return result[:1500]

    except Exception as e:
        print(f"BeautifulSoup error: {e}")
        return ""


# ══════════════════════════════════════════════════════════
#  STEP 3: Fetch top URLs with Playwright + Trafilatura
# ══════════════════════════════════════════════════════════

# Sites that block scraping — skip them
SKIP_DOMAINS = {
    "instagram.com", "tiktok.com", "twitter.com", "x.com",
    "linkedin.com", "facebook.com", "youtube.com",
    "pinterest.com", "reddit.com", "quora.com",
}

async def fetch_and_extract_urls(urls: list[str], max_urls: int = 3) -> tuple[str, list[str]]:
    """Fetch top URLs using Playwright + Trafilatura"""
    buffer = []
    used_sources = []

    # Filter out blocked domains and duplicates
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

    # Fetch up to max_urls concurrently
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


async def _fetch_one_url(url: str) -> str:
    """Try simple HTTP first, fallback to Playwright for JS sites"""
    html = ""

    # Try simple HTTP first (faster)
    try:
        async with httpx.AsyncClient(
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
            follow_redirects=True
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                html = resp.text
    except:
        pass

    # If empty or JS-blocked, try Playwright
    if not html or len(html) < 500:
        html = await scrape_with_playwright(url)

    if not html:
        return ""

    return extract_clean_text(html, url)


# ══════════════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.post("/research", response_model=ResearchResponse)
async def research(req: ResearchRequest):
    """
    Full 3-step research pipeline:
    1. SerpAPI → real Google results
    2. Playwright + Trafilatura → clean article content
    3. Return structured data for LLM analysis
    """
    if not req.serpapi_key:
        raise HTTPException(status_code=400, detail="serpapi_key is required")

    platform_str = ", ".join(req.platforms)

    try:
        # Step 1: Google search
        serp_data, urls = await fetch_serp_results(
            serpapi_key=req.serpapi_key,
            title=req.title,
            goal=req.goal,
            platforms=platform_str,
            max_queries=req.max_searches,
        )

        if not serp_data:
            return ResearchResponse(
                success=False,
                data="",
                error="SerpAPI returned no results. Check your API key."
            )

        # Step 2: Fetch and extract URL content
        url_content, sources = await fetch_and_extract_urls(urls, max_urls=3)

        # Combine all data
        final_data = serp_data
        if url_content:
            final_data += f"\n\n=== EXTRACTED WEBSITE CONTENT ===\n{url_content}"

        return ResearchResponse(
            success=True,
            data=final_data,
            sources=sources,
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scrape")
async def scrape_url(req: ScrapeRequest):
    """
    Scrape a single URL and return clean text.
    Uses Playwright + Trafilatura.
    """
    try:
        html = await scrape_with_playwright(req.url)
        if not html:
            # fallback to simple HTTP
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(req.url)
                html = resp.text

        clean = extract_clean_text(html, req.url)
        return {"success": True, "content": clean, "url": req.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
