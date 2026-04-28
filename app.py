"""
IssueScope
환경변수: ANTHROPIC_API_KEY (필수), GNEWS_KEY (선택)
실행: pip install flask requests && python app.py
"""
import os, re, json, xml.etree.ElementTree as ET
import requests
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
    data = requests.get(f"https://gnews.io/api/v4/search?q={requests.utils.quote(query)}&lang=ko&country=kr&max=10&apikey={key}", timeout=12).json()
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

def call_claude(prompt, max_tokens=1500):
    if not CLAUDE_KEY: raise ValueError("ANTHROPIC_API_KEY 미설정")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type":"application/json","x-api-key":CLAUDE_KEY,"anthropic-version":"2023-06-01"},
        json={"model":MODEL,"max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]},
        timeout=30
    )
    resp.raise_for_status()
    text = next((c["text"] for c in resp.json().get("content",[]) if c.get("type")=="text"), "")
    return text.replace("```json","").replace("```","").strip()

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    body = request.get_json(force=True)
    query = body.get("query","").strip()
    arts  = body.get("articles",[])
    if not query or not arts: return jsonify({"error":"query, articles 필요"}), 400

    txt = "\n\n".join(f"[{a['id']}] \"{a['title']}\" ({a['source']})\n{a.get('snippet') or a.get('description','')}" for a in arts[:25])
    ids = ", ".join(a["id"] for a in arts[:25])
    prompt = f"""한국 뉴스 분석 전문가. "{query}" 키워드 최신 기사 {min(len(arts),25)}건:

{txt}

JSON만 반환 (마크다운 없이):
{{"overallSummary":"2-3문장. 현재 상황과 핵심 흐름을 구체적으로 요약","mainIssues":[{{"id":"i1","title":"이슈 제목 20자 이내","desc":"2-3문장. 무슨 일이 왜 중요한지 구체적 사실 기반 서술","refs":["기사id"],"sev":"high|medium|low"}}]}}

기사 ID: {ids}
한국어. 3~5개 이슈."""
    try:
        parsed = json.loads(call_claude(prompt))
        return jsonify({"summary":parsed.get("overallSummary",""),
                        "issues":[{"id":x.get("id",""),"title":x.get("title",""),
                                   "desc":x.get("desc") or x.get("description",""),
                                   "refs":x.get("refs") or [],"sev":x.get("sev","medium")}
                                  for x in parsed.get("mainIssues",[])]})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/detail", methods=["POST"])
def api_detail():
    body = request.get_json(force=True)
    query, title, snippet = body.get("query",""), body.get("title",""), body.get("snippet","")
    prompt = f"""기사 분석. 검색 주제: "{query}"
제목: {title}
내용: {snippet}

JSON만 반환 (마크다운 없이):
{{"bullets":["핵심 내용 4~5개. 구체적 사실·수치·주체 포함. 무슨 일인지 서술."],"rel":"'{query}'와 구체적으로 어떻게 연관되는지 1~2문장. 키워드 언급 표현 금지. 실제 인과관계·영향 메커니즘 서술.","impl":["시사점 2~3개. 구체적으로."]}}
한국어."""
    try:
        return jsonify(json.loads(call_claude(prompt, 800)))
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/status")
def api_status():
    return jsonify({"claude": bool(CLAUDE_KEY), "model": MODEL if CLAUDE_KEY else None})

@app.route("/")
def index():
    return open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅  http://localhost:{port}")
    print(f"   Claude: {'✅ 연결됨 ('+MODEL+')' if CLAUDE_KEY else '❌ ANTHROPIC_API_KEY 없음'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
