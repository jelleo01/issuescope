"""
IssueScope
환경변수:
  ANTHROPIC_API_KEY  (Claude - 선택)
  GEMINI_API_KEY     (Google Gemini Flash - 선택, 무료)
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET (뉴스 수집 - 선택)
"""
import os, re, json, xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

CLAUDE_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
NAVER_ID    = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SEC   = os.environ.get("NAVER_CLIENT_SECRET", "")

# 사용할 LLM 자동 선택 (Gemini 우선 - 무료)
def get_llm():
    if GEMINI_KEY:   return "gemini"
    if CLAUDE_KEY:   return "claude"
    return None

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, text/xml, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://news.google.com/",
}
CRAWL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.google.com/",
}

# ─── 크롤링 ──────────────────────────────────────────────────────────────────

BODY_SELECTORS = [
    "#dic_area", "#articleBodyContents", "#articleBody",
    "#article-view-content-div", "#newsct_article",
    ".article_view", ".article-content", ".article_content",
    ".view_cont", "#articeBody", "#newsBody",
    "[itemprop='articleBody']", "article",
]

GOOGLE_NEWS_RE = re.compile(r"news\.google\.com")

def resolve_google_url(url, timeout=8):
    """Google News URL이면 스킵, 아니면 그대로 반환"""
    if not url:
        return ""
    if GOOGLE_NEWS_RE.search(url):
        # RSS description에서 이미 실제 URL을 추출했어야 함
        # 혹시 구글 URL이 오면 GET으로 한 번 더 시도
        try:
            r = requests.get(url, headers=CRAWL_HEADERS, timeout=timeout, allow_redirects=True)
            for resp in r.history:
                loc = resp.headers.get("Location","")
                if loc and "google.com" not in loc and loc.startswith("http"):
                    return loc
            if "google.com" not in r.url:
                return r.url
            # HTML에서 실제 URL 추출 시도
            urls = re.findall(r'href="(https?://(?!news\.google\.com)[^"]+)"', r.text)
            if urls:
                return urls[0]
        except Exception as e:
            print(f"[resolve] error: {e}")
        return ""
    return url

def crawl_body(url, timeout=8):
    """기사 URL → 본문 텍스트"""
    if not url or url == "#":
        return ""
    # Google News URL이면 실제 URL로 변환
    real_url = resolve_google_url(url, timeout=6)
    if not real_url:
        return ""
    print(f"[crawl] fetching: {real_url[:80]}")
    try:
        resp = requests.get(real_url, headers=CRAWL_HEADERS, timeout=timeout, allow_redirects=True)
        if not resp.ok or len(resp.text) < 500:
            print(f"[crawl] failed: {resp.status_code}")
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script","style","nav","header","footer","aside","iframe","noscript","figure"]):
            tag.decompose()
        for sel in BODY_SELECTORS:
            el = soup.select_one(sel)
            if el:
                t = re.sub(r"\s+", " ", el.get_text()).strip()
                if len(t) > 200:
                    print(f"[crawl] got {len(t)} chars via '{sel}'")
                    return t[:3000]
        # fallback: p 태그
        paras = [p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 50]
        if paras:
            text = re.sub(r"\s+", " ", " ".join(paras[:12]))[:3000]
            print(f"[crawl] got {len(text)} chars via <p> tags")
            return text
        print("[crawl] no body found")
    except Exception as e:
        print(f"[crawl] error: {e}")
    return ""

def crawl_parallel(articles, n=8):
    """상위 n개 기사 병렬 크롤링"""
    results = {}
    def fetch(a):
        return a["id"], crawl_body(a.get("url",""))
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch, a): a["id"] for a in articles[:n]}
        for f in as_completed(futs, timeout=15):
            try:
                aid, body = f.result()
                results[aid] = body
            except Exception:
                pass
    return results

# ─── 뉴스 수집 ──────────────────────────────────────────────────────────────

def clean(text):
    if not text: return ""
    text = re.sub(r"<[^>]+>", "", text)
    for h, r in [("&lt;","<"),("&gt;",">"),("&amp;","&"),("&quot;",'"'),("&#39;","'"),("&nbsp;"," ")]:
        text = text.replace(h, r)
    return re.sub(r"\s+", " ", text).strip()

def fetch_google_rss(query, max_items=40):
    # 영어 뉴스 RSS (description에 실제 기사 URL이 HTML entity로 포함됨)
    url = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en".format(
        requests.utils.quote(query))
    resp = requests.get(url, headers=RSS_HEADERS, timeout=12)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for i, item in enumerate(root.findall(".//item")[:max_items]):
        raw = item.findtext("title") or ""
        title = re.sub(r" - [^-]+$", "", clean(raw)).strip()
        if not title: continue

        # description은 HTML entity encoded → ET가 자동 디코딩
        desc_raw = item.findtext("description") or ""

        if i < 2:
            print(f"[rss-debug] desc: {repr(desc_raw[:300])}")

        # 실제 기사 URL 추출 (google/lh3/gstatic 제외)
        real_urls = re.findall(
            r'href="(https?://(?!(?:news\.google|lh3\.google|google\.|gstatic\.)[^"]*)[^"]+)"',
            desc_raw)
        link = real_urls[0] if real_urls else ""

        pub = item.findtext("pubDate") or ""
        # description에서 텍스트만 추출
        desc_text = re.sub(r"<[^>]+>", "", desc_raw).strip()[:200]

        src_el = item.find("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text
                  else (re.search(r" - ([^-]+)$", raw) or [None,"알 수 없음"])[1])

        print(f"[rss] '{title[:45]}' → {link[:70] or 'NO URL'}")
        items.append({"title":title,"source":source,"url":link,
                      "publishedAt":pub,"description":desc_text})
    return items

def fetch_naver(query, n=20):
    if not NAVER_ID: raise ValueError("no naver key")
    resp = requests.get(
        "https://openapi.naver.com/v1/search/news.json",
        params={"query":query,"display":n,"sort":"date"},
        headers={"X-Naver-Client-Id":NAVER_ID,"X-Naver-Client-Secret":NAVER_SEC},
        timeout=10)
    resp.raise_for_status()
    items = []
    for a in resp.json().get("items",[]):
        host = re.search(r"https?://([^/]+)", a.get("originallink","") or "")
        items.append({
            "title": clean(a.get("title","")),
            "source": host.group(1) if host else "네이버뉴스",
            "url": a.get("originallink") or a.get("link",""),
            "publishedAt": a.get("pubDate",""),
            "description": clean(a.get("description",""))[:400],
        })
    return items

@app.route("/api/news")
def api_news():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error":"q 필요","items":[]}), 400
    try: return jsonify({"items": fetch_google_rss(q), "source":"google"})
    except Exception: pass
    try: return jsonify({"items": fetch_naver(q), "source":"naver"})
    except Exception as e: return jsonify({"error":str(e),"items":[]}), 500

# ─── LLM 호출 ────────────────────────────────────────────────────────────────

def call_gemini(prompt, max_tokens=1500):
    """Google Gemini 2.0 Flash - 무료, 빠름"""
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={}".format(GEMINI_KEY)
    resp = requests.post(url,
        json={"contents":[{"parts":[{"text": prompt}]}],
              "generationConfig":{"maxOutputTokens": max_tokens, "temperature": 0.3}},
        timeout=40)
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
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
    raise ValueError("LLM 키 없음 - GEMINI_API_KEY 또는 ANTHROPIC_API_KEY 설정 필요")

# ─── 분석 API ────────────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body  = request.get_json(force=True)
    query = body.get("query","").strip()
    arts  = body.get("articles",[])
    if not query or not arts:
        return jsonify({"error":"query, articles 필요"}), 400

    # 1단계: 상위 8개 기사 본문 크롤링
    print(f"[analyze] Crawling {min(len(arts),8)} articles for '{query}'...")
    bodies = crawl_parallel(arts, n=8)
    success = sum(1 for v in bodies.values() if v)
    print(f"[analyze] Crawled: {success}/{min(len(arts),8)} articles got body text")

    # 2단계: 각 기사 콘텐츠 구성
    blocks = []
    for a in arts[:8]:
        body_text = bodies.get(a["id"],"").strip()
        content   = body_text or a.get("description","") or ""
        label     = "본문" if len(body_text) > 100 else "요약"
        blocks.append(
            "[{}] {} ({})\n{}: {}".format(
                a["id"], a["title"], a["source"], label,
                content[:1500] if content else "(내용 없음)"
            )
        )

    content_str = "\n\n---\n\n".join(blocks)
    has_body = any(bodies.get(a["id"],"") for a in arts[:8])

    prompt = """당신은 한국 경제/금융 뉴스 애널리스트입니다.
검색 주제: "{query}"

아래 기사들을 분석하세요:

{content}

JSON만 반환 (마크다운 없이):
{{
  "overallSummary": "'{query}'를 둘러싼 현재 상황 3문장. {body_note}",
  "mainIssues": [
    {{
      "id": "i1",
      "title": "이슈 제목 20자 이내",
      "desc": "이 이슈의 내용과 시장·투자자 관점 의미 2~3문장. {body_note}",
      "refs": ["기사id"],
      "sev": "high|medium|low"
    }}
  ]
}}
한국어. 3~5개 이슈.""".format(
        query=query,
        content=content_str,
        body_note="기사 본문에서 확인된 구체적 사실(기업명·수치·날짜)만 사용. 추측·지어내기 금지." if has_body
                  else "기사 제목에서 파악되는 사실만 서술. 확인 안된 내용 절대 추가 금지."
    )

    try:
        parsed = json.loads(call_llm(prompt, 1500))
        return jsonify({
            "summary": parsed.get("overallSummary",""),
            "issues":  [{"id":x.get("id",""),"title":x.get("title",""),
                         "desc":x.get("desc") or x.get("description",""),
                         "refs":x.get("refs",[]),"sev":x.get("sev","medium")}
                        for x in parsed.get("mainIssues",[])]
        })
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/detail", methods=["POST"])
def api_detail():
    body   = request.get_json(force=True)
    query  = body.get("query","")
    title  = body.get("title","")
    url    = body.get("url","")

    # 기사 본문 크롤링
    print(f"[detail] Crawling: {url[:80]}")
    content = crawl_body(url)
    print(f"[detail] Got {len(content)} chars")

    if not content:
        return jsonify({
            "bullets": ["기사 본문을 가져올 수 없습니다. 원문 링크를 직접 확인해주세요."],
            "rel": "", "impl": []
        })

    prompt = """한국 경제/금융 뉴스 분석. 검색 주제: "{}"

기사 제목: {}
기사 본문:
{}

JSON만 반환 (마크다운 없이):
{{
  "bullets": ["본문에서 확인된 핵심 사실 4~5개. 인물/기업/수치/날짜 포함. 본문에 없는 내용 절대 추가 금지."],
  "rel": "'{}'와 어떻게 직접 연결되는지 1~2문장. 실제 인과관계 서술. '키워드 언급' 표현 금지.",
  "impl": ["시사점 2~3개. 본문 근거."]
}}
한국어.""".format(query, title, content[:3000], query)

    try:
        return jsonify(json.loads(call_llm(prompt, 1000)))
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/test-crawl")
def test_crawl():
    """Railway 서버에서 한국 뉴스 사이트 접근 가능 여부 테스트"""
    test_urls = [
        "https://www.digitaltoday.co.kr",
        "https://www.hankyung.com",
        "https://www.mk.co.kr",
        "https://n.news.naver.com",
        "https://www.yna.co.kr",
    ]
    results = {}
    for url in test_urls:
        try:
            r = requests.get(url, headers=CRAWL_HEADERS, timeout=6, allow_redirects=True)
            results[url] = {"status": r.status_code, "bytes": len(r.content)}
        except Exception as e:
            results[url] = {"error": str(e)}
    return jsonify(results)
    return jsonify({
        "llm":    get_llm(),
        "gemini": bool(GEMINI_KEY),
        "claude": bool(CLAUDE_KEY),
        "naver":  bool(NAVER_ID),
    })

@app.route("/")
def index():
    return open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    llm  = get_llm()
    print(f"\n http://localhost:{port}")
    print(f"   LLM   : {llm or 'NONE - API key 필요'}")
    print(f"   Naver : {'on' if NAVER_ID else 'off'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)

@app.route("/api/test-url")
def test_url():
    """특정 URL 크롤링 테스트"""
    url = request.args.get("url","")
    if not url:
        return jsonify({"error": "url 파라미터 필요"})
    real_url = resolve_google_url(url)
    try:
        r = requests.get(real_url or url, headers=CRAWL_HEADERS, timeout=10, allow_redirects=True)
        body = crawl_body(real_url or url)
        return jsonify({
            "input_url": url,
            "resolved_url": real_url,
            "final_url": r.url,
            "status": r.status_code,
            "bytes": len(r.content),
            "body_chars": len(body),
            "body_preview": body[:500] if body else None
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/status")
def api_status():
    return jsonify({
        "llm":    get_llm(),
        "gemini": bool(GEMINI_KEY),
        "claude": bool(CLAUDE_KEY),
        "naver":  bool(NAVER_ID),
    })
