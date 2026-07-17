"""
線3 語意搜尋——索引器：掃 input/videos 全部影片，逐字稿＋向量進 SQLite。

- 來源只讀不動；索引與衍生資料全存 D:\\VideoAI\\search\\
- 增量式：檔案指紋（大小+mtime）沒變就跳過，跑第二次只處理新增檔
- 逐字稿走 speech.py（faster-whisper 本地），與線2b 產製共用同一份快取——
  產製時轉過的影片建索引零重工
- 向量用 Azure text-embedding-3-small；沒設 API key 時照樣建索引（純關鍵字搜尋）
- 文章標題從 jobs.jsonl 的產製紀錄反查（影片檔名 → 當時的文章標題），一併可搜

CLI：python indexer.py           # 增量建索引
     python indexer.py --stats   # 只看目前索引統計
"""
import json
import re
import sqlite3
import struct
import sys
import datetime
from pathlib import Path

BASE       = Path(r"D:\VideoAI")
VIDEOS_DIR = BASE / "input" / "videos"
SEARCH_DIR = BASE / "search"
DB_FILE    = SEARCH_DIR / "index.db"
SOURCES_FILE = SEARCH_DIR / "sources.json"
_ENV_FILE  = BASE / ".env"
_JOBS_LOG  = BASE / "logs" / "jobs.jsonl"

# 預設來源；要納入 NAS/其他影庫，直接改 search/sources.json 加一筆
_DEFAULT_SOURCES = [
    {"name": "input", "path": str(VIDEOS_DIR), "enabled": True},
]


def load_sources() -> list[dict]:
    """讀來源清單（search/sources.json）；不存在就建預設檔"""
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    if not SOURCES_FILE.exists():
        SOURCES_FILE.write_text(json.dumps(_DEFAULT_SOURCES, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    try:
        srcs = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except Exception:
        srcs = list(_DEFAULT_SOURCES)
    out = []
    for s in srcs:
        if not s.get("enabled", True):
            continue
        p = Path(s.get("path", ""))
        if not s.get("name") or not p.exists():
            print(f"  ⚠ 來源略過（名稱缺漏或路徑不存在）：{s}")
            continue
        out.append({"name": s["name"], "path": p})
    return out

# 逐字稿切塊參數：滑動視窗太碎沒意義、太長語意稀釋
CHUNK_MAX_SEC   = 32.0   # 一塊最長秒數
CHUNK_MAX_CHARS = 120    # 一塊最多字元（超過就切）
CHUNK_GAP_SEC   = 3.0    # 語句間隔超過就切塊（通常是換話題/換人）

# Whisper 對無人聲片段的已知幻覺句（舊快取可能殘留），整塊等於這些就丟
_HALLUCINATIONS = {"請用繁體中文", "字幕由Amara.org社群提供", "謝謝觀看", "謝謝大家收看"}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts", ".ts"}


def _load_env():
    """讀 D:\\VideoAI\\.env（跟 app.py 同一份），indexer 單獨跑時也有 API key"""
    import os
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _connect() -> sqlite3.Connection:
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=10)
    # WAL：讓「寫索引時搜尋站仍能讀」不互鎖（搜尋站是純讀端）；busy_timeout 遇短暫鎖會等而非立刻報錯
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=8000")
    # 舊版 schema（無 src 欄）直接砍掉重建——逐字稿/校正/向量都有快取，重建近乎免費
    cols = {r[1] for r in con.execute("PRAGMA table_info(videos)")}
    if cols and "src" not in cols:
        con.execute("DROP TABLE IF EXISTS segments")
        con.execute("DROP TABLE IF EXISTS videos")
        print("  舊版索引 schema，砍掉重建（快取都在，不會重轉譯）")
    con.execute("""CREATE TABLE IF NOT EXISTS videos(
        id INTEGER PRIMARY KEY,
        src TEXT DEFAULT 'input',
        relpath TEXT,
        size INTEGER, mtime INTEGER,
        duration REAL DEFAULT 0,
        title TEXT DEFAULT '',
        status TEXT DEFAULT '',
        indexed_at TEXT DEFAULT '',
        UNIQUE(src, relpath))""")
    con.execute("""CREATE TABLE IF NOT EXISTS segments(
        id INTEGER PRIMARY KEY,
        video_id INTEGER NOT NULL,
        start REAL, end REAL,
        text TEXT,
        text_raw TEXT DEFAULT '',
        source TEXT DEFAULT 'speech',
        embedding BLOB)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_seg_video ON segments(video_id)")
    return con


# ── 向量打包：float32 binary，比存 JSON 小 3 倍、載入快 ─────────────────────

def pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vec(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


# ── 文章標題反查：產製紀錄裡「用過哪些影片」→ 影片檔名對到文章標題 ───────────

def _title_map() -> dict[str, str]:
    m: dict[str, str] = {}
    if not _JOBS_LOG.exists():
        return m
    for line in _JOBS_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("type") != "produce":
            continue
        title = (r.get("title") or "").strip()
        if not title:
            continue
        for p in (r.get("video_path") or "").split(";"):
            name = Path(p.strip()).name
            if name:
                m[name] = title   # 後面的紀錄覆蓋前面（取最新一次產製的標題）
    return m


# ── 逐字稿切塊 ───────────────────────────────────────────────────────────────

def chunk_transcript(segments: list[dict]) -> list[dict]:
    """whisper segments → 檢索用 chunk（約 30 秒/120 字一塊，遇長停頓另起）"""
    chunks: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text = "".join(s["text"] for s in cur).strip()
        core = re.sub(r"[^0-9A-Za-z一-鿿]", "", text)
        if len(core) >= 4 and core not in {re.sub(r"[^0-9A-Za-z一-鿿]", "", h)
                                           for h in _HALLUCINATIONS}:
            chunks.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text})
        cur.clear()

    for s in segments:
        if cur:
            span  = s["end"] - cur[0]["start"]
            chars = sum(len(x["text"]) for x in cur) + len(s["text"])
            gap   = s["start"] - cur[-1]["end"]
            if span > CHUNK_MAX_SEC or chars > CHUNK_MAX_CHARS or gap > CHUNK_GAP_SEC:
                flush()
        cur.append(s)
    flush()
    return chunks


# ── 主流程 ───────────────────────────────────────────────────────────────────

def _probe_duration(video: Path) -> float:
    import subprocess
    from select_clip import _ffprobe_exe
    try:
        r = subprocess.run(
            [_ffprobe_exe(), "-v", "quiet", "-print_format", "json",
             "-show_format", str(video)],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        return round(float(json.loads(r.stdout)["format"]["duration"]), 2)
    except Exception:
        return 0.0


def _correct_texts(video: Path, texts: list[str]) -> list[str]:
    """
    GPT 校正逐字稿：同音錯字/簡體字/明顯誤聽（實測例：「以為物質升級用路能安全」
    →「以維護自身及用路人安全」、「普仲基」→「普重機」）。
    關鍵字比對非常吃這個。結果進快取（ns speechfix1），重建索引不重付。
    """
    if not texts:
        return texts
    from select_clip import _cache_get, _cache_put
    cached = _cache_get("speechfix1", video)
    if cached is not None and len(cached.get("texts", [])) == len(texts):
        return cached["texts"]
    try:
        import os
        from select_clip import _azure_client
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        resp = _azure_client().chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2"),
            messages=[{"role": "user", "content":
                "你是台灣新聞台的文字編輯。以下是語音辨識逐字稿片段，請逐條修正："
                "同音錯字、簡體字轉繁體、依上下文修正明顯誤聽的詞。"
                "保留口語結構、不增刪內容、長度盡量相近；人名無法確定時保留原字。\n"
                f"{numbered}\n\n"
                '回傳 JSON：{"texts": ["修正後第1條", ...]}，數量與順序必須跟輸入一致。'}],
            response_format={"type": "json_object"},
            max_completion_tokens=3000,
        )
        fixed = json.loads(resp.choices[0].message.content).get("texts", [])
        if len(fixed) == len(texts) and all(isinstance(t, str) and t.strip() for t in fixed):
            _cache_put("speechfix1", video, {"texts": fixed})
            return fixed
        print("  ⚠ 校正回傳數量不符，改用原稿")
    except Exception as e:
        print(f"  ⚠ 逐字稿校正失敗（{e}），改用原稿")
    return texts


def _embed_or_none(texts: list[str]) -> list[bytes | None]:
    """批次算向量；API 不通（沒 key/斷網）就整批回 None，索引照建（關鍵字仍可搜）"""
    try:
        from select_clip import _embed_texts
        out: list[bytes | None] = []
        for i in range(0, len(texts), 32):
            out.extend(pack_vec(v) for v in _embed_texts(texts[i:i + 32]))
        return out
    except Exception as e:
        print(f"  ⚠ 向量計算失敗（{e}），本批只建關鍵字索引")
        return [None] * len(texts)


# ── M2-lite：智慧分析（畫面視覺描述）併入索引 ────────────────────────────────
# 產製線的「智慧分析」（select_clip.catalog_video）早就把每支影片逐段看過、
# 存了畫面描述/主體/畫面文字到 catalog 快取。這裡只「讀既有快取」轉成可檢索
# 片段（source='visual'）——已分析過的影片零額外 GPT 花費即可用畫面內容搜尋。
# 沒進過產製線、從沒被智慧分析過的影片沒有這份快取，就只有語音層可搜（正常）。

def visual_segments_from_catalog(video: Path) -> list[dict]:
    """讀 catalog 快取整理成可檢索的視覺片段；沒有快取回 []（不觸發新分析）。"""
    try:
        from select_clip import _cache_get
    except Exception:
        return []
    cat = _cache_get("catalog3", Path(video))
    if not cat or not cat.get("segments"):
        return []
    out: list[dict] = []
    for s in cat["segments"]:
        desc = (s.get("description") or "").strip()
        subj = (s.get("subject") or "").strip()
        stext = (s.get("screen_text") or "").strip()
        # 去重保序：描述＋主體＋畫面文字串成一句（畫面文字如招牌/字卡最好搜）
        parts = [p for p in (desc, subj, stext)
                 if p and p not in ("（未取得描述）", "（無法抽取影格）")]
        text = "，".join(dict.fromkeys(parts))
        if len(re.sub(r"[^0-9A-Za-z一-鿿]", "", text)) < 3:
            continue
        out.append({"start": float(s.get("start", 0) or 0),
                    "end": float(s.get("end", 0) or 0), "text": text})
    # 相鄰且描述完全相同的段落合併成一個時間跨度，減少洗版與向量筆數
    merged: list[dict] = []
    for s in out:
        if merged and merged[-1]["text"] == s["text"]:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    return merged


def sync_visual_segments(con, video_id: int, video: Path) -> int:
    """確保該影片的視覺描述片段已進索引（冪等）；已存在就不重做，回傳新增筆數。"""
    have = con.execute("SELECT COUNT(*) FROM segments WHERE video_id=? AND source='visual'",
                       (video_id,)).fetchone()[0]
    if have:
        return 0
    vsegs = visual_segments_from_catalog(video)
    if not vsegs:
        return 0
    embs = _embed_or_none([v["text"] for v in vsegs])
    cur = con.cursor()
    for v, e in zip(vsegs, embs):
        cur.execute("""INSERT INTO segments(video_id, start, end, text, text_raw,
                       source, embedding) VALUES(?,?,?,?,?,?,?)""",
                    (video_id, v["start"], v["end"], v["text"], "", "visual", e))
    con.commit()
    return len(vsegs)


def run_index(progress=None) -> dict:
    """
    增量建索引。progress 是選配 callback(done, total, msg)。
    回傳 {"scanned","added","skipped","removed","errors"}
    """
    _load_env()
    import speech   # 延後 import：載入 whisper 相關模組較慢

    def report(done, total, msg):
        if progress:
            try:
                progress(done, total, msg)
            except Exception:
                pass
        print(f"[{done}/{total}] {msg}", flush=True)

    con = _connect()
    titles = _title_map()
    sources = load_sources()
    all_files: list[tuple[str, Path, Path]] = []   # (來源名, 根目錄, 檔案)
    for s in sources:
        all_files += [(s["name"], s["path"], p) for p in sorted(s["path"].rglob("*"))
                      if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    stats = {"scanned": len(all_files), "added": 0, "skipped": 0, "removed": 0,
             "errors": 0, "visual": 0}

    # 磁碟上已不存在的（被清掉/搬走/來源停用）→ 索引一併移除
    on_disk = {(src, str(f.relative_to(root))) for src, root, f in all_files}
    gone = [(rid,) for rid, src, rel in
            con.execute("SELECT id, src, relpath FROM videos")
            if (src, rel) not in on_disk]
    if gone:
        con.executemany("DELETE FROM segments WHERE video_id=?", gone)
        con.executemany("DELETE FROM videos WHERE id=?", gone)
        stats["removed"] = len(gone)

    for i, (src, root, f) in enumerate(all_files, 1):
        rel = str(f.relative_to(root))
        st = f.stat()
        row = con.execute("SELECT id, size, mtime, status FROM videos WHERE src=? AND relpath=?",
                          (src, rel)).fetchone()
        if row and row[1] == st.st_size and row[2] == int(st.st_mtime) and row[3] in ("ok", "no_speech"):
            # 檔案沒變，但產製紀錄可能新對到標題了 → 順手補
            title = titles.get(f.name, "")
            if title:
                con.execute("UPDATE videos SET title=? WHERE id=?", (title, row[0]))
            # M2-lite：這支影片可能在上次建索引之後才被拿去產製（多了智慧分析
            # 快取），檔案本身沒變也要把新出現的視覺描述補進索引
            nv = sync_visual_segments(con, row[0], f)
            if nv:
                stats["visual"] += nv
                report(i, len(all_files), f"補視覺描述 [{src}] {rel}（{nv} 段）")
            stats["skipped"] += 1
            continue

        report(i, len(all_files), f"處理 [{src}] {rel}")
        try:
            duration = _probe_duration(f)
            tr = speech.transcribe(f)          # 有快取：產製轉過的直接命中
            chunks = chunk_transcript(tr.get("segments", []))
            title = titles.get(f.name, "")

            raw_texts = [c["text"] for c in chunks]
            texts = _correct_texts(f, raw_texts)   # GPT 校正錯字（有快取）
            if title:   # 標題也做成一個可檢索片段（涵蓋整支影片）
                texts = texts + [title]
            embs = _embed_or_none(texts) if texts else []
            # 有文字卻整批拿不到向量＝暫時性向量 API 失敗：標 partial 讓下次重建重試，
            # 不要標 ok 被永久略過而凍在「只有關鍵字、無語意搜尋」狀態（B9）。
            emb_failed = bool(texts) and not any(e is not None for e in embs)
            status = "partial" if emb_failed else ("ok" if chunks else "no_speech")

            cur = con.cursor()
            if row:
                cur.execute("DELETE FROM segments WHERE video_id=?", (row[0],))
                cur.execute("""UPDATE videos SET size=?, mtime=?, duration=?, title=?,
                               status=?, indexed_at=? WHERE id=?""",
                            (st.st_size, int(st.st_mtime), duration, title, status,
                             datetime.datetime.now().isoformat(timespec="seconds"), row[0]))
                vid = row[0]
            else:
                cur.execute("""INSERT INTO videos(src, relpath, size, mtime, duration, title,
                               status, indexed_at) VALUES(?,?,?,?,?,?,?,?)""",
                            (src, rel, st.st_size, int(st.st_mtime), duration, title, status,
                             datetime.datetime.now().isoformat(timespec="seconds")))
                vid = cur.lastrowid
            for c, raw, txt, e in zip(chunks, raw_texts, texts, embs):
                cur.execute("""INSERT INTO segments(video_id, start, end, text, text_raw,
                               source, embedding) VALUES(?,?,?,?,?,?,?)""",
                            (vid, c["start"], c["end"], txt, raw, "speech", e))
            if title:
                cur.execute("""INSERT INTO segments(video_id, start, end, text, text_raw,
                               source, embedding) VALUES(?,?,?,?,?,?,?)""",
                            (vid, 0.0, duration, title, "", "title",
                             embs[-1] if embs else None))
            con.commit()
            stats["added"] += 1
            # M2-lite：若這支影片有智慧分析快取，畫面描述一併進索引（免費共用）
            stats["visual"] += sync_visual_segments(con, vid, f)
        except Exception as e:
            print(f"  ✗ [{src}] {rel}: {e}", flush=True)
            stats["errors"] += 1

    con.commit()
    con.close()
    report(len(all_files), len(all_files),
           f"完成：新增/更新 {stats['added']}、略過 {stats['skipped']}、"
           f"移除 {stats['removed']}、失敗 {stats['errors']}、"
           f"視覺描述 {stats['visual']} 段")
    return stats


def index_stats() -> dict:
    if not DB_FILE.exists():
        return {"videos": 0, "segments": 0, "with_vec": 0, "no_speech": 0, "last": ""}
    con = sqlite3.connect(DB_FILE, timeout=10)
    con.execute("PRAGMA busy_timeout=8000")
    try:
        v  = con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        s  = con.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        wv = con.execute("SELECT COUNT(*) FROM segments WHERE embedding IS NOT NULL").fetchone()[0]
        ns = con.execute("SELECT COUNT(*) FROM videos WHERE status='no_speech'").fetchone()[0]
        last = con.execute("SELECT MAX(indexed_at) FROM videos").fetchone()[0] or ""
        return {"videos": v, "segments": s, "with_vec": wv, "no_speech": ns, "last": last}
    finally:
        con.close()


if __name__ == "__main__":
    if "--stats" in sys.argv:
        print(json.dumps(index_stats(), ensure_ascii=False, indent=2))
    else:
        run_index()
