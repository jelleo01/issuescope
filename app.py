"""
IssueScope
환경변수: GEMINI_API_KEY (분석), NEWSAPI_KEY (뉴스 수집)
"""
import os, re, json
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
CLAUDE_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY","") or os.environ.get("NEWS_API_KEY","")

def get_llm():
    if GEMINI_KEY: return "gemini"
    if CLAUDE_KEY: return "claude"
    return None

CRAWL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# ─── 뉴스 수집: NewsAPI.org ─────────────────────────────────────────────────

def fetch_newsapi(query, max_items=30):
    if not NEWSAPI_KEY:
        raise ValueError("NEWSAPI_KEY 미설정")
    resp = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": min(max_items, 100),
            "apiKey": NEWSAPI_KEY,
        },
        timeout=12,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise ValueError(data.get("message","NewsAPI error"))
    arts = data.get("articles") or []
    if not arts:
        raise ValueError("No results")
    items = []
    for a in arts:
        if a.get("title","") == "[Removed]":
            continue
        items.append({
            "title": a.get("title",""),
            "source": (a.get("source") or {}).get("name","Unknown"),
            "url": a.get("url",""),
            "publishedAt": a.get("publishedAt",""),
            "description": (a.get("description") or "")[:400],
            "content": (a.get("content") or "")[:500],
        })
    print(f"[newsapi] Got {len(items)} articles for '{query}'")
    return items

@app.route("/api/news")
def api_news():
    q = request.args.get("q","").strip()
    if not q:
        return jsonify({"error":"q required","items":[]}), 400
    try:
        items = fetch_newsapi(q)
        return jsonify({"items": items, "source": "newsapi"})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500

# ─── 크롤링 ─────────────────────────────────────────────────────────────────

BODY_SELECTORS = [
    "article", "[itemprop='articleBody']",
    ".article-body", ".article-content", ".article__body",
    ".story-body", ".story-content",
    ".post-content", ".entry-content",
    "#article-body", "#story", ".caas-body",
    "main",
]

def crawl_body(url, timeout=8):
    if not url or url == "#":
        return ""
    try:
        resp = requests.get(url, headers=CRAWL_HEADERS, timeout=timeout, allow_redirects=True)
        if not resp.ok or len(resp.text) < 500:
            print(f"[crawl] {resp.status_code} for {url[:60]}")
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script","style","nav","header","footer","aside","iframe","noscript","figure","figcaption"]):
            tag.decompose()
        for sel in BODY_SELECTORS:
            el = soup.select_one(sel)
            if el:
                t = re.sub(r"\s+", " ", el.get_text()).strip()
                if len(t) > 300:
                    print(f"[crawl] {len(t)} chars via '{sel}' from {url[:50]}")
                    return t[:4000]
        paras = [p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 40]
        if paras:
            text = re.sub(r"\s+", " ", " ".join(paras[:15]))[:4000]
            print(f"[crawl] {len(text)} chars via <p> from {url[:50]}")
            return text
        print(f"[crawl] no body from {url[:50]}")
    except Exception as e:
        print(f"[crawl] error {e} for {url[:50]}")
    return ""

def crawl_parallel(articles, n=8):
    results = {}
    def fetch(a):
        return a["id"], crawl_body(a.get("url",""))
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch, a): a["id"] for a in articles[:n]}
        for f in as_completed(futs, timeout=20):
            try:
                aid, body = f.result()
                results[aid] = body
            except Exception:
                pass
    return results

# ─── LLM ─────────────────────────────────────────────────────────────────────

def call_gemini(prompt, max_tokens=1500):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={}".format(GEMINI_KEY)
    resp = requests.post(url,
        json={"contents":[{"parts":[{"text": prompt}]}],
              "generationConfig":{"maxOutputTokens": max_tokens, "temperature": 0.3}},
        timeout=40)
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return text.replace("```json","").replace("```","").strip()

def call_claude(prompt, max_tokens=1500):
    resp = requests.post("https://api.anthropic.com/v1/messages",
        headers={"Content-Type":"application/json","x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01"},
        json={"model":"claude-haiku-4-5-20251001","max_tokens":max_tokens,
              "messages":[{"role":"user","content":prompt}]},
        timeout=40)
    resp.raise_for_status()
    text = next((c["text"] for c in resp.json().get("content",[]) if c.get("type")=="text"), "")
    return text.replace("```json","").replace("```","").strip()

def call_llm(prompt, max_tokens=1500):
    llm = get_llm()
    if llm == "gemini": return call_gemini(prompt, max_tokens)
    if llm == "claude": return call_claude(prompt, max_tokens)
    raise ValueError("No LLM key set - add GEMINI_API_KEY or ANTHROPIC_API_KEY")

# ─── 분석 API ────────────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body  = request.get_json(force=True)
    query = body.get("query","").strip()
    arts  = body.get("articles",[])
    if not query or not arts:
        return jsonify({"error":"query, articles required"}), 400

    # 상위 8개 기사 본문 크롤링
    top = arts[:10]
    print(f"[analyze] Crawling {len(top)} articles for '{query}'...")
    bodies = crawl_parallel(top, n=10)
    success = sum(1 for v in bodies.values() if v)
    print(f"[analyze] Crawled: {success}/{len(top)} got body text")

    blocks = []
    for a in top:
        body_text = bodies.get(a["id"],"").strip()
        # NewsAPI content (260 chars) as middle fallback
        api_content = a.get("content","").strip()
        desc = a.get("description","").strip()
        content = body_text or api_content or desc or ""
        label = "FULL" if len(body_text) > 300 else ("PARTIAL" if content else "TITLE ONLY")
        blocks.append("[{}] {} ({})\n[{}] {}".format(
            a["id"], a["title"], a["source"], label,
            content[:2000] if content else a["title"]))

    content_str = "\n\n---\n\n".join(blocks)

    prompt = """You are a senior investigative journalist and analyst. You have been given {n} articles about "{query}".

Your job: Read every article carefully. Then produce a REAL analysis — not a list of headlines, not a generic summary. Tell me what is ACTUALLY happening, who the key players are, what the conflicts are, what the stakes are, and what might happen next.

ARTICLES:
{content}

Return JSON only (no markdown, no commentary):
{{
  "overallSummary": "Write 5-8 sentences. This must read like the opening of a deeply researched briefing document. Start with the single most important development. Then explain the broader context. Include specific company names, dollar amounts, dates, percentages — whatever the articles contain. Explain WHY this matters to investors/industry/public. End with the key tension or unresolved question. Do NOT just list article titles. Do NOT use phrases like 'several articles discuss' or 'multiple sources report'. Write as if you are briefing a CEO.",
  "mainIssues": [
    {{
      "id": "i1",
      "title": "Crisp issue title, max 8 words",
      "desc": "4-6 sentences. Explain: (1) What specifically happened (2) Who is involved and what are their positions (3) Why this matters — financial impact, regulatory implications, competitive dynamics, or public consequences (4) What to watch next. Use concrete facts from the articles. No filler, no generic statements.",
      "refs": ["article_ids that support this issue"],
      "sev": "high|medium|low"
    }}
  ]
}}

RULES:
- 4-6 issues, each with substantial descriptions
- Every claim must come from the articles provided
- If articles contain numbers, quotes, or dates — USE THEM
- Do not pad with generic observations
- Write in English
""".format(n=len(top), query=query, content=content_str)

    try:
        parsed = json.loads(call_llm(prompt, 3000))
        return jsonify({
            "summary": parsed.get("overallSummary",""),
            "issues": [{"id":x.get("id",""),"title":x.get("title",""),
                        "desc":x.get("desc") or x.get("description",""),
                        "refs":x.get("refs",[]),"sev":x.get("sev","medium")}
                       for x in parsed.get("mainIssues",[])]
        })
    except Exception as e:
        print(f"[analyze] LLM error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/detail", methods=["POST"])
def api_detail():
    body   = request.get_json(force=True)
    query  = body.get("query","")
    title  = body.get("title","")
    url    = body.get("url","")
    snippet = body.get("snippet","")

    print(f"[detail] Crawling: {url[:80]}")
    content = crawl_body(url)
    print(f"[detail] Got {len(content)} chars")

    if not content:
        content = snippet or ""
    if not content:
        return jsonify({"bullets":["Could not fetch article. Click the link to read directly."],"rel":"","impl":[]})

    prompt = """You are a senior analyst producing a briefing on this article.

Search topic: "{query}"
Article title: {title}
Article text ({chars} characters):
{content}

Read the ENTIRE article carefully. Then return JSON only (no markdown):
{{
  "bullets": [
    "Write 5-10 bullet points. Each bullet should be a COMPLETE sentence (15-30 words). Cover every major point in the article — key facts, decisions, numbers, quotes, context, and implications. Do NOT just restate the headline. Do NOT truncate. Do NOT skip important details. Go through the article paragraph by paragraph and extract what matters."
  ],
  "rel": "2-3 sentences explaining exactly how this article connects to '{query}'. Describe the specific mechanism: Does it affect pricing? Regulation? Competition? Supply chain? Investment flows? Consumer behavior? Be concrete.",
  "impl": [
    "3-4 forward-looking implications. What does this mean for the market, for competitors, for investors, for policy? Each should be a specific, actionable insight — not generic filler like 'this could have major implications'."
  ]
}}
""".format(query=query, title=title, chars=len(content), content=content[:4000])

    try:
        return jsonify(json.loads(call_llm(prompt, 2000)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/test-crawl")
def test_crawl():
    tests = [
        "https://www.reuters.com",
        "https://techcrunch.com",
        "https://www.bbc.com/news",
        "https://www.cnbc.com",
        "https://www.theverge.com",
    ]
    results = {}
    for u in tests:
        try:
            r = requests.get(u, headers=CRAWL_HEADERS, timeout=6, allow_redirects=True)
            results[u] = {"status": r.status_code, "bytes": len(r.content)}
        except Exception as e:
            results[u] = {"error": str(e)}
    return jsonify(results)

@app.route("/api/test-url")
def test_url():
    url = request.args.get("url","")
    if not url:
        return jsonify({"error": "url param required"})
    body = crawl_body(url)
    return jsonify({"url": url, "body_chars": len(body), "preview": body[:500] if body else None})

@app.route("/api/status")
def api_status():
    return jsonify({"llm": get_llm(), "gemini": bool(GEMINI_KEY),
                    "claude": bool(CLAUDE_KEY), "newsapi": bool(NEWSAPI_KEY)})

@app.route("/")
def index():
    return open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    llm  = get_llm()
    print(f"\n http://localhost:{port}")
    print(f"   LLM    : {llm or 'NONE - set GEMINI_API_KEY'}")
    print(f"   NewsAPI: {'on' if NEWSAPI_KEY else 'off - set NEWSAPI_KEY'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
