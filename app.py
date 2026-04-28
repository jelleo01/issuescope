"""
IssueScope — 뉴스 이슈 분석기
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
로컬 실행 (맥/PC):
  pip install flask requests
  python app.py  →  http://localhost:5000

Railway/Render 클라우드 배포:
  환경변수 GNEWS_KEY=<gnews.io 무료키> 설정
"""
import os, re, xml.etree.ElementTree as ET
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

HEADERS = {
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
    resp = requests.get(url, headers=HEADERS, timeout=12)
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
                link = (child.tail or child.text or "").strip()
                break
        pub = item.findtext("pubDate") or ""
        desc = clean(item.findtext("description") or "")[:300]
        src_el = item.find("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text
                  else (re.search(r" - ([^-]+)$", raw) or [None, "알 수 없음"])[1])
        items.append({"title": title, "source": source, "url": link, "publishedAt": pub, "description": desc})
    return items

def fetch_gnews(query):
    key = os.environ.get("GNEWS_KEY", "").strip()
    if not key: raise ValueError("GNEWS_KEY 환경변수 없음")
    url = f"https://gnews.io/api/v4/search?q={requests.utils.quote(query)}&lang=ko&country=kr&max=10&apikey={key}"
    resp = requests.get(url, timeout=12)
    data = resp.json()
    if "errors" in data: raise ValueError(str(data["errors"]))
    articles = data.get("articles") or []
    if not articles: raise ValueError("결과 없음")
    return [{"title": a.get("title",""), "source": (a.get("source") or {}).get("name","알 수 없음"),
             "url": a.get("url",""), "publishedAt": a.get("publishedAt",""),
             "description": (a.get("description") or "")[:300]} for a in articles]

@app.route("/api/news")
def api_news():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q 파라미터 필요", "items": []}), 400
    try:
        items = fetch_google_rss(q)
        return jsonify({"query": q, "count": len(items), "items": items})
    except Exception:
        pass
    try:
        items = fetch_gnews(q)
        return jsonify({"query": q, "count": len(items), "items": items})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500

@app.route("/")
def index():
    return open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
