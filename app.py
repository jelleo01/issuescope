"""
IssueScope
환경변수: ANTHROPIC_API_KEY (필수), GNEWS_KEY (선택), NAVER_CLIENT_ID / NAVER_CLIENT_SECRET (선택)
"""
import os, re, json, xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

app = Flask(__name__)
CLAUDE_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
NAVER_ID    = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SEC   = os.environ.get("NAVER_CLIENT_SECRET", "")
MODEL       = "claude-haiku-4-5-20251001"

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, text/xml, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://news.google.com/",
}
ARTICLE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.google.com/",
    "Cache-Control": "no-cache",
}

# ─── 기사 본문 추출 ──────────────────────────────────────────────────────────

BODY_SELECTORS = [
    # 네이버 뉴스
    "#dic_area", "#articleBodyContents",
    # 한겨레, 경향, 오마이
    ".article-text", ".article_view", ".art_txt",
    # 조선, 중앙, 동아
    "#article_body", ".article_body", "#news_body_id",
    # 한경, 매경
    "#articletxt", "#article-view-content-div", "#newsct_article",
    # 연합뉴스
    ".article", "#articleBody",
    # 일반
    "[itemprop='articleBody']", "article", ".content-body",
    ".post-body", "#content", ".entry-content",
]

def extract_body(html):
    """HTML에서 기사 본문 추출"""
    soup = BeautifulSoup(html, 'lxml')
    # 불필요한 태그 제거
    for tag in soup(['script','style','nav','header','footer','aside',
                     'iframe','noscript','figure','figcaption','form']):
        tag.decompose()

    for sel in BODY_SELECTORS:
        el = soup.select_one(sel)
        if el:
            text = re.sub(r'\s+', ' ', el.get_text()).strip()
            if len(text) > 200:
                return text[:3000]

    # fallback: 50자 이상 <p> 태그 모으기
    paras = [p.get_text().strip() for p in soup.find_all('p') if len(p.get_text().strip()) > 50]
    if paras:
        return re.sub(r'\s+', ' ', ' '.join(paras[:15]))[:3000]

    return ""

def fetch_article_body(url, timeout=8):
    """기사 URL에서 본문 가져오기"""
    if not url or url == '#':
        return ""
    try:
        resp = requests.get(url, headers=ARTICLE_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.ok and len(resp.text) > 500:
            return extract_body(resp.text)
    except Exception:
        pass
    return ""

def enrich_articles_parallel(articles, max_workers=6, timeout_per=8):
    """기사 목록 본문 병렬 fetch"""
    results = {a['id']: "" for a in articles}

    def fetch_one(a):
        return a['id'], fetch_article_body(a.get('url',''), timeout_per)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_one, a): a['id'] for a in articles}
        for fut in as_completed(futures, timeout=timeout_per + 2):
            try:
                aid, body = fut.result()
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
    return text.strip()

def fetch_google_rss(query, max_items=40):
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    resp = requests.get(url, headers=RSS_HEADERS, timeout=12)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall(".//item")[:max_items]:
        raw = item.findtext("title") or ""
        title = re.sub(r" - [^-]+$", "", clean(raw)).strip()
        if not title: continue
        link = ""
        for child in item:
            if child.tag == "link":
                link = (child.tail or child.text or "").strip(); break
        pub = item.findtext("pubDate") or ""
        desc = clean(item.findtext("description") or "")[:200]
        src_el = item.find("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text
                  else (re.search(r" - ([^-]+)$", raw) or [None,"알 수 없음"])[1])
        items.append({"title":title,"source":source,"url":link,"publishedAt":pub,"description":desc})
    return items

def fetch_naver_news(query, max_items=20):
    """Naver 뉴스 API - description에 본문 일부 포함"""
    if not NAVER_ID or not NAVER_SEC:
        raise ValueError("Naver API 키 없음")
    resp = requests.get(
        "https://openapi.naver.com/v1/search/news.json",
        params={"query": query, "display": max_items, "sort": "date"},
        headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SEC},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    items = []
    for i, a in enumerate(data.get("items", [])):
        title = clean(a.get("title",""))
        desc  = clean(a.get("description",""))[:400]  # Naver는 본문 200-400자 포함
        items.append({
            "title": title, "source": clean(a.get("originallink","").split("/")[2] if a.get("originallink") else "네이버뉴스"),
            "url": a.get("originallink") or a.get("link",""),
            "publishedAt": a.get("pubDate",""), "description": desc
        })
    return items

@app.route("/api/news")
def api_news():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error":"q 필요","items":[]}), 400
    
    # 1) Google RSS 시도
    try:
        items = fetch_google_rss(q)
        return jsonify({"query":q,"items":items,"source":"google"})
    except Exception: pass
    
    # 2) Naver API 시도
    try:
        items = fetch_naver_news(q)
        return jsonify({"query":q,"items":items,"source":"naver"})
    except Exception as e:
        return jsonify({"error":str(e),"items":[]}), 500

# ─── Claude ─────────────────────────────────────────────────────────────────

def call_claude(prompt, max_tokens=1500):
    if not CLAUDE_KEY: raise ValueError("ANTHROPIC_API_KEY 미설정")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type":"application/json","x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01"},
        json={"model":MODEL,"max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]},
        timeout=40
    )
    resp.raise_for_status()
    text = next((c["text"] for c in resp.json().get("content",[]) if c.get("type")=="text"), "")
    return text.replace("```json","").replace("```","").strip()

# ─── 분석 엔드포인트 ─────────────────────────────────────────────────────────

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body  = request.get_json(force=True)
    query = body.get("query","").strip()
    arts  = body.get("articles",[])
    if not query or not arts:
        return jsonify({"error":"query, articles 필요"}), 400

    # 상위 10개 기사 본문 병렬 fetch
    top = arts[:10]
    bodies = enrich_articles_parallel(top, max_workers=6)

    # 각 기사 콘텐츠 구성 (본문 있으면 본문, 없으면 description)
    article_blocks = []
    for a in top:
        body_text = bodies.get(a['id'],"").strip()
        content   = body_text if body_text else a.get('description','') or ''
        if content:
            article_blocks.append(
                f"[{a['id']}] 제목: {a['title']} (출처: {a['source']})\n"
                f"{'본문' if body_text else '요약'}: {content[:1500]}"
            )
        else:
            article_blocks.append(f"[{a['id']}] 제목: {a['title']} (출처: {a['source']})")

    content_str = "\n\n---\n\n".join(article_blocks)
    has_body = any(v for v in bodies.values())

    prompt = f"""당신은 한국 경제·금융 전문 뉴스 애널리스트입니다.
검색 주제: "{query}"

{"아래는 실제 기사 본문입니다:" if has_body else "아래는 기사 제목 및 요약입니다:"}

{content_str}

위 내용을 바탕으로 JSON만 반환하세요 (마크다운 없이):
{{
  "overallSummary": "'{query}'를 둘러싼 현재 상황 3문장. 기사에서 확인된 구체적 사실(기업명·수치·날짜·사건)만 사용. 추측 금지.",
  "mainIssues": [
    {{
      "id": "i1",
      "title": "이슈 제목 20자 이내",
      "desc": "이 이슈의 내용과 시장·투자자·규제 관점에서의 의미 2~3문장. 기사 내용 기반으로만 작성. 확인되지 않은 내용 금지.",
      "refs": ["기사id"],
      "sev": "high|medium|low"
    }}
  ]
}}
한국어. 3~5개 이슈."""

    try:
        parsed = json.loads(call_claude(prompt, 1500))
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
    body    = request.get_json(force=True)
    query   = body.get("query","")
    title   = body.get("title","")
    url     = body.get("url","")
    snippet = body.get("snippet","")

    # 실제 본문 fetch 시도
    content = fetch_article_body(url)
    if not content:
        content = snippet or ""

    if not content:
        return jsonify({"bullets":["기사 본문을 가져올 수 없습니다. 원문 링크를 직접 확인해주세요."],"rel":"","impl":[]}), 200

    prompt = f"""한국 금융·경제 뉴스 애널리스트. 검색 주제: "{query}"

기사 제목: {title}
{'기사 본문' if len(content) > 100 else '기사 요약'}:
{content[:3000]}

JSON만 반환 (마크다운 없이):
{{
  "bullets": ["기사 본문에서 확인된 핵심 사실 4~5개. 각각 구체적 인물·기업·수치·날짜 포함. 본문에 없는 내용 절대 추가 금지."],
  "rel": "이 기사가 '{query}'와 어떻게 직접 연결되는지 1~2문장. 실제 인과관계·영향 메커니즘 서술. '키워드 언급' 표현 금지.",
  "impl": ["이 기사가 시사하는 투자·정책·시장 관점 전망 2~3개. 본문 내용 기반으로."]
}}
한국어."""

    try:
        return jsonify(json.loads(call_claude(prompt, 1000)))
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/status")
def api_status():
    return jsonify({"claude": bool(CLAUDE_KEY), "model": MODEL if CLAUDE_KEY else None,
                    "naver": bool(NAVER_ID and NAVER_SEC)})

@app.route("/")
def index():
    return open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅  http://localhost:{port}")
    print(f"   Claude : {'✅ ' + MODEL if CLAUDE_KEY else '❌ ANTHROPIC_API_KEY 없음'}")
    print(f"   Naver  : {'✅' if NAVER_ID else '❌ NAVER_CLIENT_ID 없음 (선택)'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
