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
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

from produce import BASE, _ffprobe_exe


def _download_atomic(url: str, dest: Path, timeout: int, headers: dict):
    """串流下載到 <dest>.part 完成後才原子改名成 dest，避免中斷留半檔被下次
    exists() 誤判為已完成（後續轉譯/探測會讀到壞檔）；分塊寫入也避免大檔整檔載入記憶體。"""
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
            shutil.copyfileobj(resp, f, length=1 << 20)   # 1MB 一塊
        os.replace(tmp, dest)                              # 同碟原子改名
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)                    # 失敗殘檔清掉

INPUT = BASE / "input"
PROBE_CACHE = BASE / "logs" / "probe_cache.json"
_probe_lock = threading.Lock()
PHOTO_COUNT_CACHE = BASE / "logs" / "photo_count_cache.json"
_photo_count_lock = threading.Lock()

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
    """回傳 {content, keywords, source_url, photo_url, photo_caption}"""
    data = _get_json(ARTICLE_API.format(article_no=article_no))
    raw = data.get("LTNA_Content", "")
    keywords = [t.get("Name", "") for t in data.get("NewsArticleTag", []) if t.get("Name")]
    a_photo = data.get("A_Photo") or {}
    photo_url = a_photo.get("PathL", "")
    # A_Photo.Content 是記者手寫的主圖圖說（如「鄭姓竊賊…被警方逮捕送辦。（記者○○翻攝）」）。
    # 早期 API 沒給，只能靠 GPT 看圖猜；現在有了就優先用——準確、免費、還帶事件脈絡。
    photo_caption = (a_photo.get("Content") or "").strip()
    return {"content": _clean_content(raw), "keywords": keywords,
            "source_url": data.get("LTRT_Url", ""), "photo_url": photo_url,
            "photo_caption": photo_caption}


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


def _load_photo_count_cache() -> dict:
    if PHOTO_COUNT_CACHE.exists():
        try:
            return json.loads(PHOTO_COUNT_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def get_photo_count(article_no: str) -> int | None:
    """
    文章配圖張數，探測一次後永久快取（依 articleNo）。已發布文章的配圖不會再變，
    不用像影片長度那樣可能因素材更新而重探——原本每次評估都重打文章 API＋逐號
    HEAD 探測，是後臺清單評估緩慢的主因之一。探測失敗回 None、不快取，下次重試。
    """
    with _photo_count_lock:
        cache = _load_photo_count_cache()
    if article_no in cache:
        return cache[article_no]
    try:
        art = fetch_article(article_no)
        n = len(discover_photos(art["photo_url"])) if art.get("photo_url") else 0
    except Exception:
        return None
    with _photo_count_lock:
        cache = _load_photo_count_cache()
        cache[article_no] = n
        PHOTO_COUNT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PHOTO_COUNT_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return n


PHOTO_CAPTION_CACHE = BASE / "input" / "photos" / "_captions.json"
_photo_caption_lock = threading.Lock()


def save_photo_caption(article_no: str, caption: str):
    """把記者手寫的主圖圖說（A_Photo.Content）存進 sidecar，配對時直接讀、免再打 API。"""
    caption = (caption or "").strip()
    if not caption:
        return
    with _photo_caption_lock:
        cache = {}
        if PHOTO_CAPTION_CACHE.exists():
            try:
                cache = json.loads(PHOTO_CAPTION_CACHE.read_text(encoding="utf-8"))
            except Exception:
                cache = {}
        cache[str(article_no)] = caption
        PHOTO_CAPTION_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PHOTO_CAPTION_CACHE.write_text(json.dumps(cache, ensure_ascii=False),
                                      encoding="utf-8")


def get_photo_caption(photo_path: str) -> str:
    """
    依本機照片路徑回傳記者圖說。只有主圖（檔名 photo-{articleNo}，無 -N 後綴）有圖說；
    探測抓來的其餘配圖（photo-{articleNo}-2…）API 沒給圖說，回空字串讓上層 fallback GPT。
    """
    m = re.match(r"^photo-(\d+)$", Path(photo_path).stem)
    if not m:
        return ""
    with _photo_caption_lock:
        if not PHOTO_CAPTION_CACHE.exists():
            return ""
        try:
            cache = json.loads(PHOTO_CAPTION_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return ""
    return cache.get(m.group(1), "")


def download_photo(url: str, article_no: str, idx: int = 1) -> str:
    """下載新聞配圖到 input/photos/，回傳本機路徑；已存在就跳過重下"""
    photo_dir = INPUT / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(url.split("?")[0]).suffix or ".jpg"
    dest = photo_dir / (f"photo-{article_no}{ext}" if idx == 1
                        else f"photo-{article_no}-{idx}{ext}")
    if not dest.exists():
        _download_atomic(url, dest, timeout=60, headers=_UA)
    return str(dest)


def download_video(url: str) -> str:
    """下載影片到 input/videos/<日期>/，回傳本機路徑；同檔名已存在就跳過重下"""
    name = url.rsplit("/", 1)[-1]
    dest_dir = INPUT / "videos" / _video_date_folder(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    if not dest.exists():
        _download_atomic(url, dest, timeout=120, headers=_UA)
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
    探不到（timeout/壞檔）回 -1，但**不寫入快取**——探測失敗常是一次性網路/timeout，
    寫死 -1 會讓明明夠長的素材永久被判「素材不足」，下次應重探而非沿用失敗值。
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
        result[url] = dur
        if dur >= 0:                 # 只快取成功探測；-1 失敗不寫、下次重探
            cache[url] = dur
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
        level, label = "yellow", "△ 素材偏少"
    else:
        level, label = "red", "✕ 素材不足"
    return {"total_sec": total, "n_videos": len(valid), "level": level, "label": label}
