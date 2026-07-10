"""
LTN 內部 API 串接：後臺影音清單、文章內容 → 匯入 VideoAI 自動化測試用。

兩支 API：
  1. getvideos：依時間區段列出後臺影音清單（含影片實際存放位置）
  2. getESNewsDetail：依 articleNo 抓文章全文＋關鍵字＋主圖

素材只用「記者上傳的影片」＋「記者放的新聞主圖」，不碰文章內嵌的網傳影片
（那走 LiTV 第三方平台，刻意不處理）。
另含：影片長度探測（ffprobe over HTTP）＋快取，供「素材夠不夠做 60 秒」預判。
"""
import json
import re
import subprocess
import threading
import urllib.request
from pathlib import Path

from produce import BASE, _ffprobe_exe

INPUT = BASE / "input"
PROBE_CACHE = BASE / "logs" / "probe_cache.json"
_probe_lock = threading.Lock()

BACKLOG_API = "https://dev55.ltn.com.tw/staff/pelin/video/program/api/getvideos/{year}/{start}/{end}"
ARTICLE_API = "https://dev55.ltn.com.tw/staff/Chris/sportstestapi/getESNewsDetail/all/breakingnews/{article_no}"

_UA = {"User-Agent": "Mozilla/5.0"}


def _get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def fetch_backlog(year: str, start: str, end: str) -> list[dict]:
    """
    後臺影音清單。year=2026，start/end=MMDDHHmm（例：07070000 = 7/7 00:00）。
    每筆的 status 欄位是後臺自己的處理狀態（實測值：已完成／不處理不存），
    前端直接拿這個欄位篩選，不需要另外在本機記一份狀態。
    """
    data = _get_json(BACKLOG_API.format(year=year, start=start, end=end))
    return (data.get("data") or {}).get("items", [])


def _clean_content(raw: str) -> str:
    """
    去掉 <div class='ltnembed'...></div> 內嵌影片標籤與其他殘留 HTML，只留純文字內文。
    （這裡清掉標籤純粹是為了拿到乾淨內文，不會去下載那支網傳影片）
    """
    text = re.sub(r"<div[^>]*class=['\"]ltnembed['\"][^>]*>.*?</div>", "", raw, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r\n", "\n").strip()
    return text


def fetch_article(article_no: str) -> dict:
    """回傳 {content, keywords, source_url, photo_url}"""
    data = _get_json(ARTICLE_API.format(article_no=article_no))
    raw = data.get("LTNA_Content", "")
    keywords = [t.get("Name", "") for t in data.get("NewsArticleTag", []) if t.get("Name")]
    photo_url = (data.get("A_Photo") or {}).get("PathL", "")
    return {"content": _clean_content(raw), "keywords": keywords,
            "source_url": data.get("LTRT_Url", ""), "photo_url": photo_url}


def _video_date_folder(name: str) -> str:
    """記者影片檔名開頭 6 碼是日期（260709-K-...），拿來當子資料夾；認不出就 misc"""
    return name[:6] if len(name) >= 6 and name[:6].isdigit() else "misc"


def discover_photos(photo_url: str, max_photos: int = 8) -> list[str]:
    """
    LTN 圖檔照編號遞增命名（….../5500760_1_1.jpg → _2_1、_3_1…），
    API 只回報第一張；用 HEAD 逐號探測抓出同篇全部配圖（不存在回 404 即停）。
    """
    m = re.match(r"^(.*_)\d+(_\d+\.[A-Za-z0-9]+)$", photo_url or "")
    if not m:
        return [photo_url] if photo_url else []
    urls = []
    for i in range(1, max_photos + 1):
        u = f"{m.group(1)}{i}{m.group(2)}"
        try:
            req = urllib.request.Request(u, method="HEAD", headers=_UA)
            urllib.request.urlopen(req, timeout=10)
            urls.append(u)
        except Exception:
            break
    return urls or ([photo_url] if photo_url else [])


def download_photo(url: str, article_no: str, idx: int = 1) -> str:
    """下載新聞配圖到 input/photos/，回傳本機路徑；已存在就跳過重下"""
    photo_dir = INPUT / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    dest = photo_dir / (f"photo-{article_no}{ext}" if idx == 1
                        else f"photo-{article_no}-{idx}{ext}")
    if not dest.exists():
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
    return str(dest)


def download_video(url: str) -> str:
    """下載影片到 input/videos/<日期>/，回傳本機路徑；同檔名已存在就跳過重下"""
    name = url.rsplit("/", 1)[-1]
    dest_dir = INPUT / "videos" / _video_date_folder(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if not dest.exists():
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=120) as resp:
            dest.write_bytes(resp.read())
    return str(dest)


# ── 影片長度探測（素材充足度預判用）──────────────────────────────────────────

def _load_probe_cache() -> dict:
    if PROBE_CACHE.exists():
        try:
            return json.loads(PROBE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def probe_durations(urls: list[str]) -> dict:
    """
    ffprobe over HTTP 探測遠端影片長度（內網 LAN 很快），URL→秒數，有快取。
    探不到（timeout/壞檔）的回 -1，快取也記 -1 避免重複嘗試。
    """
    with _probe_lock:
        cache = _load_probe_cache()
    result = {}
    dirty = False
    for url in urls:
        if url in cache:
            result[url] = cache[url]
            continue
        dur = -1.0
        try:
            r = subprocess.run(
                [_ffprobe_exe(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", url],
                capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                dur = round(float(r.stdout.strip()), 1)
        except Exception:
            pass
        cache[url] = dur
        result[url] = dur
        dirty = True
    if dirty:
        with _probe_lock:
            merged = {**_load_probe_cache(), **cache}
            PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            PROBE_CACHE.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return result


def assess_materials(video_urls: list[str]) -> dict:
    """
    「這批素材夠不夠做一支 60 秒短影音」的預判。
    回傳 {"total_sec", "n_videos", "level", "label"}
      level: green（可一鍵生成）/ yellow（偏少，會靠照片補）/ red（不足）
    判斷基準：主畫面約需 50~60 秒畫面；素材可重複使用+照片備援，
    所以 45 秒以上算充足、20~45 秒偏少、20 秒以下不足。
    """
    durs = probe_durations(video_urls)
    valid = [d for d in durs.values() if d > 0]
    total = round(sum(valid), 1)
    if total >= 45:
        level, label = "green", "⚡ 可一鍵生成"
    elif total >= 20:
        level, label = "yellow", "△ 素材偏少（會用照片補）"
    else:
        level, label = "red", "✕ 素材不足"
    return {"total_sec": total, "n_videos": len(valid), "level": level, "label": label}
