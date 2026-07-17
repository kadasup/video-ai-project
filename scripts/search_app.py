"""
線3 語意搜尋——搜尋伺服器（port 5001，跟產製系統的 5000 完全分開）。

打一句中文（例：某人講某事）→ 混合檢索：
  - 關鍵字精確比對（人名/地名/專有名詞，貪婪最長子串覆蓋率）
  - 向量語意相似度（query embedding ↔ 逐字稿 chunk embedding）
結果給影片＋時間碼＋命中逐字稿，點了直接從那一秒開始播 → 人工檢閱。

索引由 indexer.py 建（SQLite），本程式唯讀載入記憶體，索引檔更新自動重載。
"""
import datetime
import hashlib
import json
import re
import subprocess
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_file

import indexer
from indexer import BASE, DB_FILE, SEARCH_DIR, unpack_vec

THUMB_DIR = SEARCH_DIR / "thumbs"
CLIPS_DIR = SEARCH_DIR / "clips"
QUERY_LOG = SEARCH_DIR / "query_log.jsonl"
PORT = 5001

_qlog_lock = threading.Lock()


def _qlog(rec: dict):
    """查詢/點擊紀錄：之後「搜了沒中」的清單就是 M2 視覺標註的設計依據"""
    rec["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with _qlog_lock, QUERY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass

app = Flask(__name__)

# ── 索引載入（記憶體快取，db 檔 mtime 變了就重載）────────────────────────────
_idx_lock = threading.Lock()
_idx = {"mtime": 0, "rows": [], "mat": None, "videos": {}}


def _load_index():
    import sqlite3
    if not DB_FILE.exists():
        return
    mt = DB_FILE.stat().st_mtime
    with _idx_lock:
        if mt == _idx["mtime"] and _idx["rows"]:
            return
        con = sqlite3.connect(DB_FILE, timeout=10)
        # WAL＋busy_timeout：indexer 重建索引（持寫鎖）期間讀取才不會撞
        # 「database is locked」→ 搜尋 500。WAL 是 DB 檔的持久屬性、設一次即可，
        # 這裡讀端一併設保險；busy_timeout 需逐連線設。
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=8000")
        try:
            videos = {r[0]: {"src": r[1], "relpath": r[2], "duration": r[3],
                             "title": r[4] or "",
                             "date": datetime.date.fromtimestamp(r[5] or 0).isoformat()}
                      for r in con.execute(
                          "SELECT id, src, relpath, duration, title, mtime FROM videos")}
            rows, vecs = [], []
            for vid, start, end, text, source, emb in con.execute(
                    "SELECT video_id, start, end, text, source, embedding FROM segments"):
                if vid not in videos:
                    continue
                rows.append({"vid": vid, "start": start, "end": end,
                             "text": text or "", "source": source,
                             "veci": len(vecs) if emb else -1})
                if emb:
                    vecs.append(unpack_vec(emb))
        finally:
            con.close()

        mat = None
        if vecs:
            try:
                import numpy as np
                mat = np.asarray(vecs, dtype="float32")
                norm = np.linalg.norm(mat, axis=1, keepdims=True)
                norm[norm == 0] = 1e-9
                mat = mat / norm
            except ImportError:
                mat = vecs   # 無 numpy 退回純 Python（慢但可用）
        _idx.update({"mtime": mt, "rows": rows, "mat": mat, "videos": videos})


# ── 混合比對 ─────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-鿿]", "", s or "")


def _kw_cover(q: str, text: str) -> tuple[float, list[str]]:
    """貪婪最長子串覆蓋：query 有多少字元能在 text 裡連續命中（≥2 字才算）"""
    matched, covered, i, n = [], 0, 0, len(q)
    while i < n:
        hit = 0
        for L in range(min(12, n - i), 1, -1):
            if q[i:i + L] in text:
                hit = L
                break
        if hit:
            matched.append(q[i:i + hit])
            covered += hit
            i += hit
        else:
            i += 1
    return (covered / n if n else 0.0), matched


def _embed_query(q: str):
    try:
        indexer._load_env()
        from select_clip import _embed_texts
        return _embed_texts([q])[0]
    except Exception:
        return None   # 沒 key/斷網 → 純關鍵字搜尋


def _expand_query(q: str) -> list[str]:
    """零命中時把查詢改寫成 2~3 種不同說法（同義詞/口語/書面），失敗回空清單"""
    try:
        import json as _json
        import os
        indexer._load_env()
        from select_clip import _azure_client
        resp = _azure_client().chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2"),
            messages=[{"role": "user", "content":
                "使用者在新聞影音庫搜尋，但這個查詢找不到結果。"
                "請把它改寫成 2~3 個「講同一件事的不同說法」（同義詞、口語↔書面、"
                "上位詞），台灣用語，每個不超過 12 字，專有名詞/人名保留原樣。\n"
                f"查詢：{q}\n"
                '回傳 JSON：{"alts": ["說法1", "說法2"]}'}],
            response_format={"type": "json_object"},
            max_completion_tokens=200,
        )
        alts = _json.loads(resp.choices[0].message.content).get("alts", [])
        return [a.strip() for a in alts if isinstance(a, str) and a.strip()
                and a.strip() != q][:3]
    except Exception:
        return []


def search(query: str, limit: int = 40, fsrc: str = "",
           dfrom: str = "", dto: str = "") -> dict:
    _load_index()
    mat, videos = _idx["mat"], _idx["videos"]
    rows = [r for r in _idx["rows"]
            if (not fsrc or videos[r["vid"]]["src"] == fsrc)
            and (not dfrom or videos[r["vid"]]["date"] >= dfrom)
            and (not dto or videos[r["vid"]]["date"] <= dto)]
    if not rows:
        return {"results": [], "mode": "empty", "low_confidence": False}

    qc = _clean(query)
    qvec = _embed_query(query)
    mode = "hybrid" if qvec is not None and mat is not None else "keyword"

    sims = None
    if mode == "hybrid":
        try:
            import numpy as np
            qv = np.asarray(qvec, dtype="float32")
            qv = qv / (np.linalg.norm(qv) or 1e-9)
            sims = _idx["mat"] @ qv
        except ImportError:
            from select_clip import _cosine
            sims = [_cosine(qvec, v) for v in mat]

    scored = []
    for r in rows:
        kw, terms = _kw_cover(qc, _clean(r["text"]))
        sem = float(sims[r["veci"]]) if sims is not None and r["veci"] >= 0 else 0.0
        # 語意分佔底、關鍵字覆蓋加權；連續命中 ≥3 字（多半是人名）再加碼
        score = 0.55 * sem + 0.6 * kw + (0.15 if any(len(t) >= 3 for t in terms) else 0)
        scored.append((score, sem, kw, terms, r))
    scored.sort(key=lambda x: -x[0])

    # 強結果＝關鍵字有命中、或語意分 ≥0.35（實測好命中 0.36~0.54、雜訊底噪 0.30~0.33，
    # 用綜合分當門檻會讓雜訊擦邊混進來）；全無強結果時遞補語意最接近的幾筆（低信心）
    picked = [s for s in scored if s[2] > 0 or s[1] >= 0.35]
    low_confidence = False
    if not picked:
        picked = [s for s in scored if s[1] >= 0.26][:5]
        low_confidence = bool(picked)
    scored = picked

    out, per_video = [], {}
    seen_titles = set()
    for score, sem, kw, terms, r in scored:
        if per_video.get(r["vid"], 0) >= 3:   # 單支影片最多佔 3 席，避免洗版
            continue
        if r["source"] == "title":
            # 同一篇文章的標題掛在多支素材上：標題型命中只留最高分那張，
            # 不讓 5 支素材頂同一個標題洗版（逐字稿命中不受影響）
            tkey = r["text"].strip()
            if tkey in seen_titles:
                continue
            seen_titles.add(tkey)
        per_video[r["vid"]] = per_video.get(r["vid"], 0) + 1
        v = videos[r["vid"]]
        out.append({
            "src": v["src"], "relpath": v["relpath"], "title": v["title"],
            "date": v["date"], "duration": v["duration"],
            "start": round(r["start"], 1), "end": round(r["end"], 1),
            "text": r["text"], "source": r["source"],
            "terms": [t for t in terms if len(t) >= 2],
            "score": round(score, 3), "sem": round(sem, 3), "kw": round(kw, 2),
        })
        if len(out) >= limit:
            break
    return {"results": out, "mode": mode, "low_confidence": low_confidence}


# ── 重建索引（背景執行緒）────────────────────────────────────────────────────
_reidx = {"running": False, "done": 0, "total": 0, "msg": "", "stats": None}


def _reindex_worker():
    def cb(done, total, msg):
        _reidx.update({"done": done, "total": total, "msg": msg})
    try:
        stats = indexer.run_index(progress=cb)
        _reidx["stats"] = stats
        _reidx["msg"] = "索引更新完成"
    except Exception as e:
        _reidx["msg"] = f"索引失敗：{e}"
    finally:
        _reidx["running"] = False


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": [], "mode": "empty"})
    fsrc, dfrom, dto = (request.args.get("src", ""),
                        request.args.get("dfrom", ""), request.args.get("dto", ""))
    r = search(q, fsrc=fsrc, dfrom=dfrom, dto=dto)

    # 零命中或只有低信心遞補 → GPT 換 2~3 種說法自動重搜；
    # 只有換句後拿到「強結果」（非低信心）才取代原結果
    if not r["results"] or r.get("low_confidence"):
        alts = _expand_query(q)
        if alts:
            merged, seen = [], set()
            for alt in alts:
                r2 = search(alt, fsrc=fsrc, dfrom=dfrom, dto=dto)
                if r2["results"] and not r2.get("low_confidence"):
                    for res in r2["results"]:
                        key = (res["src"], res["relpath"], res["start"])
                        if key not in seen:
                            seen.add(key)
                            merged.append(res)
            if merged:
                merged.sort(key=lambda x: -x["score"])
                r["results"] = merged[:20]
                r["expanded"] = alts
                r["low_confidence"] = False

    _qlog({"type": "search", "q": q, "mode": r["mode"],
           "expanded": r.get("expanded"),
           "weak": bool(r.get("low_confidence")),
           "n": len(r["results"]), "low_confidence": r.get("low_confidence", False),
           "top_score": r["results"][0]["score"] if r["results"] else 0,
           "src": request.args.get("src", ""),
           "dfrom": request.args.get("dfrom", ""), "dto": request.args.get("dto", "")})
    return jsonify(r)


@app.route("/api/click", methods=["POST"])
def api_click():
    d = request.get_json(silent=True) or {}
    _qlog({"type": "click", "q": d.get("q", ""), "src": d.get("src", ""),
           "relpath": d.get("relpath", ""), "start": d.get("start", 0)})
    return jsonify({"ok": True})


@app.route("/api/sources")
def api_sources():
    return jsonify([s["name"] for s in indexer.load_sources()])


@app.route("/api/stats")
def api_stats():
    return jsonify(indexer.index_stats())


@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    if _reidx["running"]:
        return jsonify({"ok": False, "msg": "索引已在進行中"})
    _reidx.update({"running": True, "done": 0, "total": 0, "msg": "啟動中…", "stats": None})
    threading.Thread(target=_reindex_worker, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/reindex_status")
def api_reindex_status():
    return jsonify(_reidx)


@app.route("/api/insights")
def api_insights():
    """query_log 聚合：最近查詢、零命中清單（M2 視覺標註的需求依據）、熱門點擊"""
    searches, clicks = [], []
    if QUERY_LOG.exists():
        for line in QUERY_LOG.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            (searches if rec.get("type") == "search" else clicks).append(rec)

    # 「沒找到」＝真零命中，或只撈到低信心遞補（使用者大概率沒找到想要的）
    zero: dict[str, dict] = {}
    for s in searches:
        if s.get("n", 0) == 0 or s.get("weak"):
            z = zero.setdefault(s["q"], {"q": s["q"], "count": 0, "last": ""})
            z["count"] += 1
            z["last"] = max(z["last"], s.get("ts", ""))
    top_click: dict[str, int] = {}
    for c in clicks:
        rp = c.get("relpath", "")
        if rp:
            top_click[rp] = top_click.get(rp, 0) + 1

    return jsonify({
        "total_searches": len(searches),
        "total_clicks": len(clicks),
        "zero_hits": sorted(zero.values(), key=lambda x: -x["count"])[:50],
        "recent": [{"ts": s.get("ts", ""), "q": s.get("q", ""), "n": s.get("n", 0),
                    "expanded": s.get("expanded")}
                   for s in reversed(searches[-30:])],
        "top_clicked": sorted(([k, v] for k, v in top_click.items()),
                              key=lambda x: -x[1])[:10],
    })


INSIGHTS_HTML = r"""<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>查詢紀錄 — 影音語意搜尋</title>
<style>
:root{--bg:#0f1420;--card:#1a2332;--line:#2a3650;--tx:#e8edf6;--tx2:#8fa1bd;--ac:#4da3ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14.5px/1.65 "Microsoft JhengHei",system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:20px;margin-bottom:2px}
a{color:var(--ac);text-decoration:none}
.sub{color:var(--tx2);font-size:12.5px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.panel h2{font-size:15px;margin-bottom:10px}
.panel.full{grid-column:1/-1}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--tx2);font-weight:400;font-size:12px}
.n0{color:#f87171;font-weight:700}
.badge{display:inline-block;background:#233046;color:var(--ac);border-radius:5px;font-size:11.5px;padding:0 7px}
.stats{display:flex;gap:20px;margin-bottom:16px;color:var(--tx2);font-size:13px}
.stats b{color:var(--tx);font-size:20px;display:block}
.empty{color:var(--tx2);padding:12px 0}
</style>
</head>
<body>
<div class="wrap">
  <h1>📈 查詢紀錄</h1>
  <div class="sub"><a href="/">← 回搜尋</a>　「搜了沒中」清單會自動累積，是之後開視覺標註（M2）時決定標什麼的依據</div>
  <div class="stats" id="stats"></div>
  <div class="grid">
    <div class="panel">
      <h2>❌ 零命中／低信心查詢（M2 需求清單）</h2>
      <div id="zero"></div>
    </div>
    <div class="panel">
      <h2>🔥 最常被點開的素材</h2>
      <div id="top"></div>
    </div>
    <div class="panel full">
      <h2>🕐 最近 30 次查詢</h2>
      <div id="recent"></div>
    </div>
  </div>
</div>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
(async()=>{
  const d=await (await fetch('/api/insights')).json();
  document.getElementById('stats').innerHTML=
    `<div><b>${d.total_searches}<\/b>總查詢<\/div><div><b>${d.total_clicks}<\/b>總點擊<\/div>`
    +`<div><b>${d.zero_hits.length}<\/b>零命中查詢<\/div>`;
  document.getElementById('zero').innerHTML=d.zero_hits.length
    ?'<table><tr><th>查詢<\/th><th>次數<\/th><th>最後一次<\/th><\/tr>'
      +d.zero_hits.map(z=>`<tr><td>${esc(z.q)}<\/td><td>${z.count}<\/td><td>${esc((z.last||'').slice(5,16).replace('T',' '))}<\/td><\/tr>`).join('')+'<\/table>'
    :'<div class="empty">目前沒有零命中——很好<\/div>';
  document.getElementById('top').innerHTML=d.top_clicked.length
    ?'<table><tr><th>素材<\/th><th>被點次數<\/th><\/tr>'
      +d.top_clicked.map(t=>`<tr><td style="word-break:break-all">${esc(t[0])}<\/td><td>${t[1]}<\/td><\/tr>`).join('')+'<\/table>'
    :'<div class="empty">還沒有點擊紀錄<\/div>';
  document.getElementById('recent').innerHTML=d.recent.length
    ?'<table><tr><th>時間<\/th><th>查詢<\/th><th>結果數<\/th><th><\/th><\/tr>'
      +d.recent.map(r=>`<tr><td>${esc((r.ts||'').slice(5,16).replace('T',' '))}<\/td><td>${esc(r.q)}<\/td>`
        +`<td class="${r.n?'':'n0'}">${r.n}<\/td>`
        +`<td>${r.expanded?'<span class="badge">🔁 已自動換句<\/span>':''}<\/td><\/tr>`).join('')+'<\/table>'
    :'<div class="empty">還沒有查詢紀錄<\/div>';
})();
</script>
</body>
</html>"""


@app.route("/insights")
def insights_page():
    return INSIGHTS_HTML


def _safe_video(src: str, rel: str) -> Path | None:
    """來源名 → 根目錄（sources.json），路徑限制在該根目錄下防穿越"""
    roots = {s["name"]: s["path"] for s in indexer.load_sources()}
    root = roots.get(src)
    if not root:
        return None
    try:
        p = (root / rel).resolve()
        p.relative_to(root.resolve())
        return p if p.is_file() else None
    except Exception:
        return None


def _req_video():
    return _safe_video(request.args.get("s", "input"), request.args.get("v", ""))


@app.route("/api/video_src")
def api_video_src():
    p = _req_video()
    if not p:
        return "not found", 404
    return send_file(str(p), mimetype="video/mp4", conditional=True)


@app.route("/api/seg_thumb")
def api_seg_thumb():
    p = _req_video()
    if not p:
        return "not found", 404
    try:
        t = max(0.0, float(request.args.get("t", "0")))
    except ValueError:
        t = 0.0
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{p}|{t:.1f}".encode()).hexdigest()
    thumb = THUMB_DIR / f"{key}.jpg"
    if not thumb.exists():
        from select_clip import _ffmpeg_exe
        subprocess.run(
            [_ffmpeg_exe(), "-y", "-ss", str(t + 0.3), "-i", str(p),
             "-vframes", "1", "-vf", "scale=320:-1", "-q:v", "5", str(thumb)],
            capture_output=True, timeout=30)
    if not thumb.exists():
        return "thumb failed", 500
    return send_file(str(thumb), mimetype="image/jpeg")


@app.route("/api/clip")
def api_clip():
    """把命中片段剪成小檔下載（前後各留 2 秒），給剪輯直接用"""
    p = _req_video()
    if not p:
        return "not found", 404
    try:
        start = max(0.0, float(request.args.get("start", "0")) - 2.0)
        end   = float(request.args.get("end", "0")) + 2.0
    except ValueError:
        return "bad range", 400
    if end <= start or end - start > 300:
        return "bad range", 400
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{p}|{start:.1f}|{end:.1f}".encode()).hexdigest()
    out = CLIPS_DIR / f"clip_{key}.mp4"
    if not out.exists():
        from select_clip import _ffmpeg_exe
        subprocess.run(
            [_ffmpeg_exe(), "-y", "-ss", str(start), "-i", str(p),
             "-t", str(end - start), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "22", "-c:a", "aac", "-movflags", "+faststart", str(out)],
            capture_output=True, timeout=180)
    if not out.exists():
        return "clip failed", 500
    dl = f"{p.stem}_{int(start)}s-{int(end)}s.mp4"
    return send_file(str(out), mimetype="video/mp4",
                     as_attachment=True, download_name=dl)


# ── 頁面 ─────────────────────────────────────────────────────────────────────

HTML = r"""<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>影音語意搜尋 — VideoAI</title>
<style>
:root{--bg:#0f1420;--card:#1a2332;--line:#2a3650;--tx:#e8edf6;--tx2:#8fa1bd;--ac:#4da3ff;--mk:#ffd54d}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:15px/1.65 "Microsoft JhengHei",system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:22px;margin-bottom:4px}
.sub{color:var(--tx2);font-size:13px;margin-bottom:18px}
.bar{display:flex;gap:8px;margin-bottom:10px}
#q{flex:1;background:var(--card);border:1px solid var(--line);border-radius:10px;
   color:var(--tx);font-size:16px;padding:12px 14px;outline:none}
#q:focus{border-color:var(--ac)}
button{background:var(--ac);border:0;border-radius:10px;color:#04121f;font-size:15px;
  font-weight:700;padding:0 22px;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--tx2);font-weight:400}
.filters{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap;align-items:center}
.filters select,.filters input{background:var(--card);border:1px solid var(--line);
  border-radius:8px;color:var(--tx);font-size:13px;padding:6px 10px;outline:none}
.filters label{color:var(--tx2);font-size:12.5px}
.clipbtn{display:inline-block;font-size:12px;color:var(--ac);border:1px solid var(--line);
  border-radius:6px;padding:1px 8px;margin-left:8px;text-decoration:none}
.clipbtn:hover{border-color:var(--ac)}
.hint{color:var(--tx2);font-size:12.5px;margin-bottom:20px}
.stats{color:var(--tx2);font-size:12.5px;margin-bottom:6px}
#idxmsg{color:var(--ac);font-size:12.5px;margin-bottom:14px;min-height:18px}
.card{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:12px;margin-bottom:12px;cursor:pointer}
.card:hover{border-color:var(--ac)}
.card img{width:170px;height:96px;object-fit:cover;border-radius:8px;background:#000;flex:none}
.card .tt{font-weight:700;font-size:15px;margin-bottom:2px}
.card .fn{color:var(--tx2);font-size:12px;margin-bottom:6px;word-break:break-all}
.card .tm{display:inline-block;background:#233046;color:var(--ac);border-radius:6px;
  font-size:12.5px;padding:1px 8px;margin-bottom:6px}
.card .tx{font-size:13.5px;color:#c6d2e6}
.card .tx mark{background:var(--mk);color:#1a1400;border-radius:3px;padding:0 2px}
.badge{display:inline-block;font-size:11px;color:var(--tx2);border:1px solid var(--line);
  border-radius:5px;padding:0 6px;margin-left:6px}
.empty{color:var(--tx2);text-align:center;padding:50px 0}
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:50;
  align-items:center;justify-content:center;flex-direction:column}
#modal video{max-width:92vw;max-height:78vh;border-radius:10px;background:#000}
#modal .mi{color:var(--tx2);font-size:13px;margin-top:10px;max-width:92vw;text-align:center}
#modal .mx{position:absolute;top:14px;right:20px;font-size:28px;color:#fff;cursor:pointer}
@media(max-width:640px){.card img{width:120px;height:68px}}
</style>
</head>
<body>
<div class="wrap">
  <h1>🔎 影音語意搜尋 <a href="/insights" style="float:right;font-size:13px;color:var(--ac);text-decoration:none;font-weight:400">📈 查詢紀錄</a></h1>
  <div class="sub">來源：input/videos 素材庫（產製短影音下載的素材，每天自動進索引）。可搜：受訪原音逐字稿＋畫面內容（產製時智慧分析過的影片，🎬 標記）</div>
  <div class="bar">
    <input id="q" placeholder="打一句話找片段，例：林榮基講跨境抓人 / 警方攔查機車" autofocus>
    <button onclick="go()">搜尋</button>
    <button class="ghost" onclick="reindex()" id="ribtn">↻ 更新索引</button>
  </div>
  <div class="filters">
    <select id="fsrc"><option value="">全部來源</option></select>
    <label>日期</label><input type="date" id="fd1"><label>～</label><input type="date" id="fd2">
    <button class="ghost" style="padding:5px 12px;font-size:12.5px" onclick="clearFilters()">清除</button>
  </div>
  <div class="hint">找「某人講某事」直接打人名＋事情；結果點了會從命中的那一秒開始播，✂ 可直接下載該片段</div>
  <div class="stats" id="stats">索引載入中…</div>
  <div id="idxmsg"></div>
  <div id="res"></div>
</div>
<div id="modal" onclick="closeModal(event)">
  <span class="mx">✕</span>
  <video id="mv" controls playsinline></video>
  <div class="mi" id="mi"></div>
</div>
<script>
const $=id=>document.getElementById(id);
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function hl(text,terms){
  let t=esc(text);
  (terms||[]).sort((a,b)=>b.length-a.length).forEach(k=>{
    if(k.length<2)return;
    t=t.split(esc(k)).join('<mark>'+esc(k)+'<\/mark>');
  });
  return t;
}
function fmt(s){s=Math.floor(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');}
async function loadStats(){
  try{
    const s=await (await fetch('/api/stats')).json();
    $('stats').textContent=`索引：${s.videos} 支影片・${s.segments} 個片段`
      +(s.no_speech?`（${s.no_speech} 支無語音）`:'')
      +(s.last?`・最近更新 ${s.last.replace('T',' ')}`:'');
  }catch(e){}
}
let _nsrc=1;
async function loadSources(){
  try{
    const arr=await (await fetch('/api/sources')).json();
    _nsrc=arr.length;
    $('fsrc').innerHTML='<option value="">全部來源<\/option>'
      +arr.map(s=>`<option value="${esc(s)}">${esc(s)}<\/option>`).join('');
  }catch(e){}
}
function clearFilters(){$('fsrc').value='';$('fd1').value='';$('fd2').value='';}
async function go(){
  const q=$('q').value.trim();
  if(!q)return;
  $('res').innerHTML='<div class="empty">搜尋中…<\/div>';
  const params=new URLSearchParams({q, src:$('fsrc').value, dfrom:$('fd1').value, dto:$('fd2').value});
  const d=await (await fetch('/api/search?'+params)).json();
  if(!d.results.length){
    $('res').innerHTML='<div class="empty">沒有找到'
      +(d.expanded?'（已自動試過：'+d.expanded.map(esc).join(' ／ ')+'）':'')
      +'，換個說法試試（或先按「更新索引」）<\/div>';
    return;
  }
  $('res').innerHTML=d.results.map(r=>{
    const qs='s='+encodeURIComponent(r.src)+'&v='+encodeURIComponent(r.relpath);
    const th='/api/seg_thumb?'+qs+'&t='+r.start;
    const cl='/api/clip?'+qs+'&start='+r.start+'&end='+r.end;
    const srcBadge=r.source==='title'?'<span class="badge">文章標題<\/span>'
      :(r.source==='visual'?'<span class="badge" style="background:#ecfccb;color:#3f6212">🎬 畫面<\/span>':'');
    const badge=srcBadge+(_nsrc>1?`<span class="badge">${esc(r.src)}<\/span>`:'');
    return `<div class="card" onclick='play(${JSON.stringify(r.src)},${JSON.stringify(r.relpath)},${r.start},${JSON.stringify(r.text)})'>
      <img loading="lazy" src="${th}">
      <div style="min-width:0">
        <div class="tt">${esc(r.title||'（未對到產製文章）')}${badge}<\/div>
        <div class="fn">${esc(r.relpath)}・${r.date||''}<\/div>
        <span class="tm">▶ ${fmt(r.start)} ~ ${fmt(r.end)}<\/span>
        <a class="clipbtn" href="${cl}" onclick="event.stopPropagation()">✂ 下載片段<\/a>
        <div class="tx">${hl(r.text,r.terms)}<\/div>
      <\/div>
    <\/div>`;
  }).join('')
    +(d.expanded?'<div class="hint">🔁 原查詢無結果，已自動換句擴大搜尋：'+d.expanded.map(esc).join(' ／ ')+'<\/div>':'')
    +(d.low_confidence?'<div class="hint">⚠ 沒有高相關結果，以下是語意最接近的候選（建議換個說法再搜一次）<\/div>':'')
    +(d.mode==='keyword'?'<div class="hint">（目前為純關鍵字模式：語意向量暫不可用）<\/div>':'');
}
function play(src,rel,start,text){
  const v=$('mv');
  v.src='/api/video_src?s='+encodeURIComponent(src)+'&v='+encodeURIComponent(rel);
  $('mi').textContent=text;
  $('modal').style.display='flex';
  v.onloadedmetadata=()=>{v.currentTime=Math.max(0,start-0.5);v.play();};
  fetch('/api/click',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({q:$('q').value.trim(),src,relpath:rel,start})}).catch(()=>{});
}
function closeModal(e){
  if(e.target.id==='mv')return;
  $('mv').pause();$('mv').src='';
  $('modal').style.display='none';
}
async function reindex(){
  const r=await (await fetch('/api/reindex',{method:'POST'})).json();
  if(!r.ok){$('idxmsg').textContent=r.msg;return;}
  $('ribtn').disabled=true;
  const timer=setInterval(async()=>{
    const s=await (await fetch('/api/reindex_status')).json();
    $('idxmsg').textContent=(s.running?'⏳ ':'✅ ')+(s.total?`[${s.done}/${s.total}] `:'')+s.msg;
    if(!s.running){clearInterval(timer);$('ribtn').disabled=false;loadStats();}
  },1500);
}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')go();});
loadStats();loadSources();
</script>
</body>
</html>"""


@app.route("/")
def home():
    return HTML


if __name__ == "__main__":
    indexer._load_env()
    print(f"影音語意搜尋 → http://localhost:{PORT}/")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
