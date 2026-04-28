"""
IssueScope
환경변수: ANTHROPIC_API_KEY (필수), GNEWS_KEY (선택)
실행: pip install flask requests trafilatura && python app.py
"""
import os, re, json, xml.etree.ElementTree as ET
import requests
import trafilatura
from flask import Flask, request, jsonify

app = Flask(__name__)
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://news.google.com/",
}

# ─── 기사 본문 추출 ──────────────────────────────────────────────────────────

def fetch_article_text(url, max_chars=3000):
    """기사 URL에서 실제 본문 텍스트 추출"""
    try:
        if not url or url == '#':
            return ""
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if text:
            return text[:max_chars]
    except Exception:
        pass
    return ""

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
        desc = clean(item.findtext("description") or "")[:300]
        src_el = item.find("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text
                  else (re.search(r" - ([^-]+)$", raw) or [None,"알 수 없음"])[1])
        items.append({"title":title,"source":source,"url":link,"publishedAt":pub,"description":desc})
    return items

def fetch_gnews(query):
    key = os.environ.get("GNEWS_KEY","").strip()
    if not key: raise ValueError("GNEWS_KEY 없음")
    data = requests.get(
        f"https://gnews.io/api/v4/search?q={requests.utils.quote(query)}&lang=ko&country=kr&max=10&apikey={key}",
        timeout=12
    ).json()
    if "errors" in data: raise ValueError(str(data["errors"]))
    arts = data.get("articles") or []
    if not arts: raise ValueError("결과 없음")
    return [{"title":a.get("title",""),"source":(a.get("source") or {}).get("name","알 수 없음"),
             "url":a.get("url",""),"publishedAt":a.get("publishedAt",""),
             "description":(a.get("description") or "")[:300]} for a in arts]

@app.route("/api/news")
def api_news():
    q = request.args.get("q","").strip()
    if not q: return jsonify({"error":"q 필요","items":[]}), 400
    try: return jsonify({"query":q,"items":fetch_google_rss(q)})
    except Exception: pass
    try: return jsonify({"query":q,"items":fetch_gnews(q)})
    except Exception as e: return jsonify({"error":str(e),"items":[]}), 500

# ─── Claude 호출 ─────────────────────────────────────────────────────────────

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
    """상위 기사 본문 fetch → 종합 이슈 분석"""
    body  = request.get_json(force=True)
    query = body.get("query","").strip()
    arts  = body.get("articles",[])
    if not query or not arts:
        return jsonify({"error":"query, articles 필요"}), 400

    # 상위 8개 기사 본문 가져오기
    enriched = []
    for a in arts[:8]:
        text = fetch_article_text(a.get("url",""))
        content = text if text else a.get("description","") or a.get("title","")
        enriched.append(f"[{a['id']}] 제목: {a['title']} (출처: {a['source']})\n본문:\n{content}")

    prompt = f"""당신은 한국 경제/금융 뉴스 전문 분석가입니다.
검색 주제: "{query}"
아래는 관련 기사 {len(enriched)}건의 실제 본문입니다.

{"="*60}
{chr(10).join(enriched)}
{"="*60}

위 기사들을 읽고 JSON만 반환하세요 (마크다운 없이):
{{
  "overallSummary": "현재 '{query}'를 둘러싼 핵심 상황을 3문장으로 요약. 구체적 사실(기업명, 수치, 날짜, 사건)을 포함할 것. 추상적 표현 금지.",
  "mainIssues": [
    {{
      "id": "i1",
      "title": "이슈 제목 20자 이내",
      "desc": "이 이슈가 왜 중요한지, 실제로 어떤 일이 벌어지고 있는지 2~3문장. 기사에서 확인된 구체적 사실 기반.",
      "refs": ["기사id"],
      "sev": "high|medium|low"
    }}
  ]
}}

규칙: 한국어. 3~5개 이슈. 기사에 없는 내용 지어내지 말 것. 제목 그대로 반복 금지."""

    try:
        parsed = json.loads(call_claude(prompt, 1500))
        return jsonify({
            "summary": parsed.get("overallSummary",""),
            "issues": [{"id":x.get("id",""),"title":x.get("title",""),
                        "desc":x.get("desc") or x.get("description",""),
                        "refs":x.get("refs",[]),"sev":x.get("sev","medium")}
                       for x in parsed.get("mainIssues",[])]
        })
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/detail", methods=["POST"])
def api_detail():
    """기사 URL에서 본문 fetch → 개별 기사 분석"""
    body    = request.get_json(force=True)
    query   = body.get("query","")
    title   = body.get("title","")
    url     = body.get("url","")

    # 실제 기사 본문 가져오기
    content = fetch_article_text(url, max_chars=4000)
    if not content:
        content = body.get("snippet","") or body.get("description","")

    if not content:
        return jsonify({"bullets":["기사 본문을 불러올 수 없습니다."],"rel":"","impl":[]}), 200

    prompt = f"""한국 뉴스 분석가. 검색 주제: "{query}"

기사 제목: {title}
기사 본문:
{content}

JSON만 반환 (마크다운 없이):
{{
  "bullets": [
    "핵심 내용 4~5개. 반드시 본문에서 확인된 구체적 사실(인물, 기업, 수치, 날짜, 사건)을 포함한 완결 문장."
  ],
  "rel": "이 기사가 '{query}'와 어떻게 직접 연관되는지 1~2문장. 실제 인과관계나 영향을 구체적으로 서술. '키워드가 언급됨' 같은 표현 절대 금지.",
  "impl": ["이 기사가 시사하는 투자·정책·시장 관점의 전망 2~3개. 각각 구체적으로."]
}}
한국어."""

    try:
        return jsonify(json.loads(call_claude(prompt, 1000)))
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/status")
def api_status():
    return jsonify({"claude": bool(CLAUDE_KEY), "model": MODEL if CLAUDE_KEY else None})

@app.route("/")
def index():
    return open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅  http://localhost:{port}")
    print(f"   Claude: {'✅ ('+MODEL+')' if CLAUDE_KEY else '❌ ANTHROPIC_API_KEY 없음'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
