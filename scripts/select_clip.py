"""
智慧選片三層篩選
Layer 1 (免費): ffmpeg 動態偵測 + 音訊 RMS 分析
Layer 2 (免費): 規則式分類（監視器偵測、音訊事件）
Layer 3 (API) : GPT-4o Vision — 只對候選片段

用法：
  python select_clip.py D:/VideoAI/input/cctv.mp4
  python select_clip.py D:/VideoAI/input/crash.mp4 --no-vision
"""

import base64
import json
import math
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional

BASE = Path(r"D:\VideoAI")
TMP  = BASE / "tmp"


# ── ffmpeg / ffprobe ──────────────────────────────────────────────────────────

def _ffmpeg_exe() -> str:
    import shutil
    f = shutil.which("ffmpeg")
    if f:
        return f
    winget = (
        r"C:\Users\kevin\AppData\Local\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe"
    )
    if Path(winget).exists():
        return winget
    raise FileNotFoundError("找不到 ffmpeg，請確認已安裝並在 PATH 中")

def _ffprobe_exe() -> str:
    import shutil
    f = shutil.which("ffprobe")
    if f:
        return f
    return _ffmpeg_exe().replace("ffmpeg.exe", "ffprobe.exe")

def _ff(*args) -> str:
    cmd = [_ffmpeg_exe(), "-y"] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stderr  # ffmpeg 輸出到 stderr

def _probe(video: Path) -> dict:
    r = subprocess.run(
        [_ffprobe_exe(), "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(video)],
        capture_output=True, text=True, encoding="utf-8"
    )
    info = json.loads(r.stdout) if r.stdout.strip() else {}
    if "format" not in info:
        raise ValueError(f"無法讀取影片資訊，請確認是有效的影片檔案：{video}")
    return info

def _duration(video: Path) -> float:
    return float(_probe(video)["format"]["duration"])


# ── Layer 1a：動態區段偵測 ────────────────────────────────────────────────────

def get_motion_segments(video: Path, threshold: float = 0.28) -> list[dict]:
    """
    用 ffmpeg scene detection 找出畫面明顯變化的時間點，聚合成候選區段。
    threshold: 0~1，越低越敏感（CCTV 靜止背景用 0.28，較寬鬆）
    """
    duration = _duration(video)

    out = _ff(
        "-i", video,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-"
    )

    change_times: list[float] = []
    for line in out.splitlines():
        if "pts_time:" in line:
            try:
                t = float(line.split("pts_time:")[1].split()[0])
                change_times.append(t)
            except (IndexError, ValueError):
                pass

    if not change_times:
        # 完全無場景變化 → 靜止監視器，整段列為一個候選
        return [{"start": 0.0, "end": min(90.0, duration),
                 "score": 0.3, "change_count": 0, "is_static": True}]

    # 聚合：間距 > 12 秒的分為不同區段
    segments: list[dict] = []
    GAP = 12.0
    seg_start = max(0.0, change_times[0] - 3.0)
    prev = change_times[0]
    count = 1

    for t in change_times[1:]:
        if t - prev > GAP:
            seg_end = min(duration, prev + 6.0)
            if seg_end - seg_start >= 3.0:
                segments.append({
                    "start": seg_start,
                    "end": seg_end,
                    "score": min(1.0, count / 6.0),
                    "change_count": count,
                    "is_static": False,
                })
            seg_start = max(0.0, t - 3.0)
            count = 1
        else:
            count += 1
        prev = t

    seg_end = min(duration, prev + 6.0)
    if seg_end - seg_start >= 3.0:
        segments.append({
            "start": seg_start,
            "end": seg_end,
            "score": min(1.0, count / 6.0),
            "change_count": count,
            "is_static": False,
        })

    return segments


# ── Layer 1b：音訊分析 ────────────────────────────────────────────────────────

def analyze_audio(video: Path) -> dict:
    """
    分析音訊：有無音軌、最大/平均音量、是否有突波（碰撞/槍聲）、突波時間點
    """
    info = _probe(video)
    audio_streams = [s for s in info.get("streams", []) if s["codec_type"] == "audio"]

    if not audio_streams:
        return {
            "has_audio": False, "has_spike": False,
            "spike_times": [], "max_vol_db": None, "mean_vol_db": None,
        }

    # volumedetect: 取整體 max/mean dB
    out = _ff("-i", video, "-af", "volumedetect", "-vn", "-f", "null", "-")
    max_vol = mean_vol = None
    for line in out.splitlines():
        if "max_volume:" in line:
            try:
                max_vol = float(line.split("max_volume:")[1].strip().split()[0])
            except Exception:
                pass
        if "mean_volume:" in line:
            try:
                mean_vol = float(line.split("mean_volume:")[1].strip().split()[0])
            except Exception:
                pass

    # 突波判定：max 比 mean 高 18dB 以上（碰撞/槍聲的特徵）
    has_spike = False
    if max_vol is not None and mean_vol is not None:
        has_spike = (max_vol - mean_vol) >= 18

    spike_times: list[float] = []
    if has_spike:
        spike_times = _find_spike_times(video)

    return {
        "has_audio": True,
        "has_spike": has_spike,
        "spike_times": spike_times,
        "max_vol_db": max_vol,
        "mean_vol_db": mean_vol,
    }


def _find_spike_times(video: Path, sample_rate: int = 8000) -> list[float]:
    """
    抽 PCM 做每秒 RMS，找出突波時間點（比平均高 3 倍以上）
    用 struct 不依賴 numpy
    """
    TMP.mkdir(parents=True, exist_ok=True)
    pcm = TMP / "_spike_check.pcm"
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-i", str(video),
         "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", str(pcm)],
        capture_output=True
    )
    if not pcm.exists():
        return []

    raw = pcm.read_bytes()
    try:
        pcm.unlink()
    except Exception:
        pass

    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw[:n * 2])

    chunk = sample_rate  # 1 秒
    rms_list: list[float] = []
    for i in range(0, len(samples), chunk):
        seg = samples[i:i + chunk]
        if len(seg) < chunk // 2:
            break
        rms = math.sqrt(sum(x * x for x in seg) / len(seg))
        rms_list.append(rms)

    if not rms_list:
        return []

    avg = sum(rms_list) / len(rms_list)
    threshold = max(avg * 3.0, 200.0)  # 至少 200 out of 32768
    return [float(i) for i, r in enumerate(rms_list) if r > threshold]


# ── Layer 2：規則式分類 ───────────────────────────────────────────────────────

def _is_cctv_style(video: Path, audio: dict) -> bool:
    """
    無音訊 = 高機率監視器（CCTV 幾乎沒麥克風）
    """
    return not audio["has_audio"]


def classify_heuristic(
    segments: list[dict],
    audio: dict,
    is_cctv: bool,
) -> list[dict]:
    """
    依規則給每個候選區段初步分類（category + confidence），不花 API
    """
    candidates: list[dict] = []

    for seg in segments:
        cat = "其他"
        conf = "low"

        if is_cctv:
            # 無聲監視器 → 預設社會事件（等 Layer 3 再細分）
            cat = "社會事件"
            conf = "medium"

        elif audio["has_audio"]:
            if audio["has_spike"]:
                # 檢查突波是否落在這個區段附近（±5 秒）
                spike_near = any(
                    seg["start"] - 5 <= t <= seg["end"] + 5
                    for t in audio["spike_times"]
                )
                if spike_near:
                    cat = "車禍或槍戰"
                    conf = "medium"
                else:
                    cat = "社會事件"
                    conf = "low"
            else:
                cat = "社會事件"
                conf = "low"

        candidates.append({
            **seg,
            "preliminary_category": cat,
            "preliminary_confidence": conf,
        })

    # score 最高的排前面
    return sorted(candidates, key=lambda x: x["score"], reverse=True)


# ── Layer 3：GPT-4o Vision ────────────────────────────────────────────────────

def _extract_frames(video: Path, start: float, count: int = 5,
                    interval: float = 2.5) -> list[Path]:
    """從 start 秒開始，每 interval 秒抽一張，共 count 張"""
    TMP.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for i in range(count):
        t = start + i * interval
        out = TMP / f"_sel_frame_{i}.jpg"
        subprocess.run(
            [_ffmpeg_exe(), "-y", "-ss", str(t), "-i", str(video),
             "-vframes", "1", "-vf", "scale=640:-1", "-q:v", "3", str(out)],
            capture_output=True
        )
        if out.exists():
            frames.append(out)
    return frames


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _azure_client():
    from openai import AzureOpenAI
    return AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    )


def describe_photo(photo: Path) -> str:
    """
    GPT 視覺辨識單張新聞照片 → 一句可供配對判讀的描述（含主體與情境）。
    LTN 的照片 API 只給網址、沒有圖說 metadata，要知道照片拍到什麼（校長？
    學童演奏？火場？）唯一辦法就是視覺辨識，配對才配得準。
    結果進快取（photodesc1），同一張重跑零花費；失敗回空字串（不擋流程）。
    """
    photo = Path(photo)
    cached = _cache_get("photodesc1", photo)
    if cached is not None:
        return cached.get("desc", "")
    try:
        content = [
            {"type": "text", "text": (
                "這是一張新聞報導配圖。用一句話（30 字內）描述畫面主體與情境，"
                "供影片剪輯配對用：具體寫出人事物與動作（例：校長在禮堂致詞、"
                "學童上台演奏豎笛、消防員在火場灌救）。畫面若有明顯身分的人物"
                "（台上致詞者、受訪者）點出來。只回純文字，不要引號或多餘說明。")},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{_b64(photo)}", "detail": "low"}},
        ]
        resp = _azure_client().chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2"),
            messages=[{"role": "user", "content": content}],
            max_completion_tokens=120,
        )
        desc = (resp.choices[0].message.content or "").strip().strip('"「」 ')
        _cache_put("photodesc1", photo, {"desc": desc})
        return desc
    except Exception as e:
        print(f"  ⚠ 照片描述失敗（{e}）")
        return ""


def classify_with_vision(
    video: Path,
    candidate: dict,
    preliminary: str,
) -> dict:
    """
    Layer 3：送 5 張連續影格給 GPT-4o，取得最終分類 + 描述 + 建議起始秒
    """
    start = candidate["start"]
    frames = _extract_frames(video, start, count=5)

    if not frames:
        return {
            **candidate,
            "category": preliminary,
            "description": "無法抽取影格",
            "confidence": "low",
            "start_sec": start,
        }

    image_parts = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{_b64(f)}",
                "detail": "low",
            },
        }
        for f in frames
    ]

    prompt = (
        f"以下是一段新聞影片素材的連續影格（從第 {start:.0f} 秒開始，"
        f"每張間隔約 2.5 秒）。\n"
        f"初步判斷提示：{preliminary}\n\n"
        "請判斷並只回傳 JSON（不含說明）：\n"
        "{\n"
        '  "category": "車禍" | "警匪槍戰" | "社會事件" | "其他",\n'
        '  "event_summary": "一句話描述畫面中發生的事情（繁體中文）",\n'
        '  "best_frame_index": 0~4,\n'
        '  "confidence": "high" | "medium" | "low"\n'
        "}"
    )

    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": prompt}] + image_parts,
        }
    ]

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _azure_client().chat.completions.create(
        model=deployment,
        messages=messages,
        response_format={"type": "json_object"},
        max_completion_tokens=256,
    )

    result = json.loads(resp.choices[0].message.content)

    token_usage = {}
    if resp.usage:
        token_usage = {
            "prompt_tokens":     resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens":      resp.usage.total_tokens,
            "frames_sent":       len(frames),
        }

    # 根據最佳影格往前 3 秒留緩衝
    best_idx = max(0, min(4, result.get("best_frame_index", 0)))
    adjusted_start = max(0.0, start + best_idx * 2.5 - 3.0)

    for f in frames:
        try:
            f.unlink()
        except Exception:
            pass

    return {
        **candidate,
        "category":    result.get("category", preliminary),
        "description": result.get("event_summary", ""),
        "confidence":  result.get("confidence", "low"),
        "start_sec":   round(adjusted_start, 1),
        "tokens":      token_usage,
    }


# ── 分析結果快取（同一支檔案不重複花 API 錢）─────────────────────────────────
_CACHE_FILE = BASE / "logs" / "analysis_cache.json"
_cache_mem: dict | None = None


def _cache_key(video: Path) -> str:
    """快取識別＝檔名＋位元組大小（M2 前置，改自舊的「完整路徑＋mtime＋大小」）。
    不含路徑與 mtime，所以同一支檔不管在哪個磁碟／機器、下載到本機還是留在 NAS，
    只要檔名與大小相同就認得出是同一支——產製線與搜尋索引可跨位置共用同一份分析。
    前提：檔名要夠獨特（LTN 命名 日期-機-稿號-W-序號 已足夠；必要時未來再加影格指紋防撞）。"""
    return f"{Path(video).name}|{Path(video).stat().st_size}"


def _cache_load() -> dict:
    global _cache_mem
    if _cache_mem is None:
        try:
            _cache_mem = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _cache_mem = {}
    return _cache_mem


def _cache_get(namespace: str, video: Path):
    try:
        return _cache_load().get(f"{namespace}:{_cache_key(video)}")
    except OSError:
        return None


def _cache_put(namespace: str, video: Path, value: dict):
    try:
        cache = _cache_load()
        cache[f"{namespace}:{_cache_key(video)}"] = value
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 快取失敗不影響主流程


# ── 近重複素材偵測（記者常同場景連拍多支，只精析一支省錢也防重複選用）────────

def _dhash_frame(video: Path, t: float) -> int | None:
    """抽 9x8 灰階影格算 dHash（64-bit 感知雜湊）——純 ffmpeg + Python，零依賴"""
    try:
        r = subprocess.run(
            [_ffmpeg_exe(), "-ss", str(t), "-i", str(video),
             "-vframes", "1", "-vf", "scale=9:8", "-pix_fmt", "gray",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=30)
        buf = r.stdout
        if len(buf) < 72:
            return None
        h = 0
        for row in range(8):
            for col in range(8):
                h = (h << 1) | (1 if buf[row * 9 + col] > buf[row * 9 + col + 1] else 0)
        return h
    except Exception:
        return None


def video_fingerprint(video: Path, duration: float) -> list[int | None]:
    """三個取樣點（25%/50%/75%）的 dHash 指紋"""
    return [_dhash_frame(video, duration * r) for r in (0.25, 0.5, 0.75)]


def near_duplicate(fp_a: list, fp_b: list, dur_a: float, dur_b: float) -> bool:
    """
    近重複判定：三個取樣點的 dHash 漢明距離都 ≤8，且長度差 <20%。
    長度條件是刻意保守——受訪 take1/take2 畫面幾乎一樣但長度常不同，
    不能因為畫面像就丟掉（音訊內容不同）。
    """
    if dur_a <= 0 or dur_b <= 0:
        return False
    if abs(dur_a - dur_b) / max(dur_a, dur_b) > 0.2:
        return False
    for a, b in zip(fp_a, fp_b):
        if a is None or b is None:
            return False
        if bin(a ^ b).count("1") > 8:
            return False
    return True


# ── 向量相似度輔助（旁白段 ↔ 畫面描述，給配對當客觀參考）────────────────────
# 用 Azure text-embedding（文字對文字）——真正的影像向量（CLIP）留給線3
# 語意搜片一起建，屆時此處無縫換成影像向量分數。

def _embed_texts(texts: list[str]) -> list[list[float]]:
    dep = os.environ.get("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    resp = _azure_client().embeddings.create(model=dep, input=texts)
    return [d.embedding for d in resp.data]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


_EMPTY_SHOT_KW = ("空景", "空鏡", "空無", "無人", "空曠", "佈置", "布置",
                  "會場一角", "布幕", "空桌", "椅子", "座位")


def _is_empty_shot(seg: dict) -> bool:
    """過場空景／無事件主體的空鏡（例：拍到講台空椅子、會場佈置、空走廊）。
    這種鏡頭當環境交代還行，但配在講事件經過的旁白上會顯得畫面空洞、像沒剪到。"""
    if seg.get("shot") == "空景":
        return True
    desc = seg.get("description", "") or ""
    subj = (seg.get("subject") or "").strip()
    if not subj and any(k in desc for k in _EMPTY_SHOT_KW):
        return True
    return any(k in desc for k in ("空無一人", "無人的", "空鏡頭"))


def seg_line(seg: dict) -> str:
    """把結構化欄位組成一行素材描述（配對 prompt 與腳本 footage_notes 共用格式）"""
    marks = []
    if seg.get("subject"):
        marks.append(f"主體:{seg['subject']}")
    if seg.get("shot"):
        marks.append(seg["shot"])
    if seg.get("quality") and seg["quality"] != "正常":
        marks.append(seg["quality"])
    if seg.get("screen_text"):
        marks.append(f"畫面字:{seg['screen_text'][:12]}")
    if seg.get("has_speech"):
        marks.append("有原音人聲")
    if _is_empty_shot(seg):
        marks.append("⚠過場空景")
    tag = f"【{'|'.join(marks)}】" if marks else ""
    return f"{seg['start']}s~{seg['end']}s：{seg.get('description', '')}{tag}"


# ── 影片內容目錄（給旁白配對用）──────────────────────────────────────────────

def _extract_frame_at(video: Path, t: float, tag: str) -> Path | None:
    """抽單一影格，檔名帶 tag 避免互相覆蓋"""
    TMP.mkdir(parents=True, exist_ok=True)
    out = TMP / f"_cat_{tag}.jpg"
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-ss", str(t), "-i", str(video),
         "-vframes", "1", "-vf", "scale=640:-1", "-q:v", "3", str(out)],
        capture_output=True
    )
    return out if out.exists() else None


def catalog_video(video: Path, max_segments: int = 16) -> dict:
    """
    幫一支影片建「內容目錄」：動態偵測切段，每段抽 2 張影格，
    一次 GPT 呼叫做結構化描述（描述/主體/鏡位/畫質/畫面文字），
    空泛段落再用高解析影格重描述一次（小主體遠景常在 low detail 下看不清）。

    回傳:
      {"path": str, "duration": float,
       "segments": [{"start", "end", "description", "subject", "shot",
                     "quality", "screen_text"}, ...],
       "tokens": {...}}
    """
    video = Path(video)

    # namespace 帶版本：結構改過（catalog2→catalog3 加結構化欄位），舊快取自動失效
    cached = _cache_get("catalog3", video)
    if cached:
        return {**cached, "tokens": {}}   # 快取命中 → 零 API 花費

    duration = _duration(video)
    raw_segments = get_motion_segments(video)

    # 靜態監視器（無場景變化）→ 均分成 2~3 段各自描述
    if len(raw_segments) == 1 and raw_segments[0].get("is_static") and duration > 20:
        third = duration / 3
        raw_segments = [
            {"start": 0.0, "end": third},
            {"start": third, "end": third * 2},
            {"start": third * 2, "end": duration},
        ]

    # 細分：記者手持連續拍攝常常沒有場景切點，整支只切出一大段，
    # 描述粒度太粗配對就不準。超過 6 秒的段落一律再切成 ~4.5 秒的小段各自描述。
    fine: list[dict] = []
    for s in raw_segments:
        seg_len = s["end"] - s["start"]
        if seg_len <= 6.0:
            fine.append(s)
        else:
            n = max(2, math.ceil(seg_len / 4.5))
            step = seg_len / n
            for k in range(n):
                fine.append({"start": s["start"] + k * step,
                             "end":   s["start"] + (k + 1) * step})
    raw_segments = fine[:max_segments]

    segments = [{"start": round(max(0.0, s["start"]), 1),
                 "end":   round(min(duration, s["end"]), 1)}
                for s in raw_segments
                if min(duration, s["end"]) - max(0.0, s["start"]) >= 1.5]
    if not segments:
        segments = [{"start": 0.0, "end": round(duration, 1)}]

    # 每段抽 2 張影格（段首+1s、段中）
    frames: list[tuple[int, Path]] = []
    for i, seg in enumerate(segments):
        mid = (seg["start"] + seg["end"]) / 2
        for j, t in enumerate([min(seg["start"] + 1.0, seg["end"]), mid]):
            f = _extract_frame_at(video, t, f"{i}_{j}")
            if f:
                frames.append((i, f))

    if not frames:
        return {"path": str(video), "duration": round(duration, 1),
                "segments": [{**s, "description": "（無法抽取影格）"} for s in segments],
                "tokens": {}}

    content: list = []
    seg_lines = "\n".join(
        f"段{i+1}：{s['start']}s ~ {s['end']}s" for i, s in enumerate(segments)
    )
    content.append({"type": "text", "text": (
        "以下是一支新聞素材影片各段落的影格（每段 2 張，依段落順序排列）。\n"
        f"段落清單：\n{seg_lines}\n\n"
        "請對每一段做結構化描述，只回傳 JSON（不含說明）：\n"
        '{"segments": [{"index": 1,\n'
        '  "description": "畫面拍到什麼（人事物與動作，具體、15 字內）",\n'
        '  "subject": "主要人事物（6 字內，例：機車拖車/警員/傷者；沒有明確主體填空字串）",\n'
        '  "subject_pos": "主體在畫面水平位置：左|中|右|滿版 擇一（滿版=主體佔滿畫面或無單一主體）",\n'
        '  "shot": "特寫|中景|遠景|空景 擇一",\n'
        '  "quality": "正常|晃動|模糊|逆光|過暗 擇一",\n'
        '  "screen_text": "畫面上可辨識的文字（招牌/字卡/車牌等，無則空字串）"\n'
        "}, ...]}\n"
        "注意：遠景裡的小主體（畫面角落的車輛/人物）也要指出來，那常是新聞事件的主角；"
        "subject_pos 要準——這決定直式裁切時要保留畫面的哪一側，抓錯主體會被裁掉。"
    )})
    for i, f in frames:
        content.append({"type": "text", "text": f"（段{i+1} 的影格）"})
        content.append({"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{_b64(f)}", "detail": "low"}})

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _azure_client().chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"},
        max_completion_tokens=2000,
    )
    result = json.loads(resp.choices[0].message.content)

    tokens = {}
    if resp.usage:
        tokens = {
            "prompt_tokens":     resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens":      resp.usage.total_tokens,
            "frames_sent":       len(frames),
        }

    info_map = {d.get("index"): d for d in result.get("segments", [])}
    for i, seg in enumerate(segments):
        d = info_map.get(i + 1, {})
        seg["description"] = d.get("description", "（未取得描述）")
        seg["subject"]     = (d.get("subject") or "").strip()
        seg["subject_pos"] = (d.get("subject_pos") or "滿版").strip()
        seg["shot"]        = (d.get("shot") or "").strip()
        seg["quality"]     = (d.get("quality") or "正常").strip()
        seg["screen_text"] = (d.get("screen_text") or "").strip()

    for _, f in frames:
        try:
            f.unlink()
        except Exception:
            pass

    # 空泛段補拍：描述含糊（空景/不明/沒有主體）的段落，改用 high detail 影格
    # 重描述一次——遠景小主體（角落的機車/人物）在 low detail 下常被看漏
    vague_idx = [i for i, s in enumerate(segments)
                 if (not s["subject"]) or
                    any(k in s["description"] for k in ("空曠", "不明", "無法辨識", "空景"))][:4]
    if vague_idx:
        content2: list = [{"type": "text", "text": (
            "以下段落先前用低解析影格描述得太空泛，請用這批高解析影格重新仔細看：\n"
            "特別注意畫面角落/遠處的小主體（車輛、人物、動作），那常是新聞主角。\n"
            "只回傳 JSON：{\"segments\": [{\"index\": 段號, \"description\": \"...\", "
            "\"subject\": \"...\", \"subject_pos\": \"左|中|右|滿版\", \"shot\": \"...\", "
            "\"quality\": \"...\", \"screen_text\": \"...\"}]}"
        )}]
        refits = []
        for i in vague_idx:
            mid = (segments[i]["start"] + segments[i]["end"]) / 2
            f = _extract_frame_at(video, mid, f"hi_{i}")
            if f:
                refits.append((i, f))
                content2.append({"type": "text", "text": f"（段{i+1} 的高解析影格）"})
                content2.append({"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{_b64(f)}", "detail": "high"}})
        if refits:
            try:
                resp2 = _azure_client().chat.completions.create(
                    model=deployment,
                    messages=[{"role": "user", "content": content2}],
                    response_format={"type": "json_object"},
                    max_completion_tokens=800,
                )
                r2 = json.loads(resp2.choices[0].message.content)
                if resp2.usage:
                    tokens["prompt_tokens"] = tokens.get("prompt_tokens", 0) + resp2.usage.prompt_tokens
                    tokens["completion_tokens"] = tokens.get("completion_tokens", 0) + resp2.usage.completion_tokens
                    tokens["total_tokens"] = tokens.get("total_tokens", 0) + resp2.usage.total_tokens
                    tokens["frames_sent"] = tokens.get("frames_sent", 0) + len(refits)
                for d in r2.get("segments", []):
                    i = int(d.get("index", 0)) - 1
                    if 0 <= i < len(segments) and d.get("description"):
                        segments[i]["description"] = d["description"]
                        segments[i]["subject"] = (d.get("subject") or segments[i]["subject"]).strip()
                        segments[i]["subject_pos"] = (d.get("subject_pos") or segments[i].get("subject_pos", "滿版")).strip()
                        segments[i]["shot"] = (d.get("shot") or segments[i]["shot"]).strip()
                        segments[i]["quality"] = (d.get("quality") or segments[i]["quality"]).strip()
                        segments[i]["screen_text"] = (d.get("screen_text") or "").strip()
            except Exception:
                pass   # 補拍失敗就用第一輪結果
            for _, f in refits:
                try:
                    f.unlink()
                except Exception:
                    pass

    result = {"path": str(video), "duration": round(duration, 1),
              "segments": segments}
    _cache_put("catalog3", video, result)
    return {**result, "tokens": tokens}


# ── 旁白 ↔ 片段 智慧配對 ─────────────────────────────────────────────────────

def _dedup_overlaps(plan: list[dict], catalogs: list[dict]) -> list[dict]:
    """
    同一支影片被取用到「重疊或幾乎相同」的區間，觀眾會看到同一段畫面連播兩次。
    prompt 規則只是提醒、GPT 常忽略——這裡硬性把後出現的片段起點位移到該片已用過
    區間之後；後面畫面不夠就往前找沒用過的空檔；整支都用光了才維持原樣。
    只動 start（畫面內容），不動 dur（總長與時間軸不變）。
    """
    cat_by_path = {str(c["path"]): c for c in catalogs}
    used: dict[str, list[tuple[float, float]]] = {}

    def _find_free_start(ranges, total, dur, want):
        """在 [0,total] 找長度 ≥dur、不與 ranges 重疊的起點，優先靠近 want。"""
        gaps, cursor = [], 0.0
        for s, e in sorted(ranges):
            if s - cursor >= dur:
                gaps.append((cursor, s))
            cursor = max(cursor, e)
        if total - cursor >= dur:
            gaps.append((cursor, total))
        if not gaps:
            return None
        best = min(gaps, key=lambda g: abs(max(g[0], min(want, g[1] - dur)) - want))
        return round(max(best[0], min(want, best[1] - dur)), 2)

    for e in plan:
        p = e.get("path")
        if not p:
            continue
        key = str(p)
        s, d = e["start"], e["dur"]
        en = s + d
        prev = used.get(key, [])
        # 重疊判定留 0.3s 容差（相鄰片段接在一起不算重複）
        overlap = [(us, ue) for us, ue in prev if s < ue - 0.3 and en > us + 0.3]
        if overlap:
            cat = cat_by_path.get(key)
            total = cat["duration"] if cat else en
            push = round(max(ue for _, ue in overlap), 2)
            if push + d <= total + 0.05:              # 往後推排得下
                s, en = push, round(push + d, 2)
            else:                                      # 後面不夠 → 找空檔
                alt = _find_free_start(prev, total, d, s)
                if alt is not None:
                    s, en = alt, round(alt + d, 2)
            e["start"] = round(s, 2)
        used.setdefault(key, []).append((s, en))
    return plan


def match_narration_to_clips(
    narration_segments: list[dict],
    catalogs: list[dict],
    total_sec: float,
    photos: list[dict] | None = None,
    notes: str | None = None,
) -> tuple[list[dict], dict]:
    """
    narration_segments: [{"start": 0, "end": 18, "desc": "..."}] 來自 generate_script
    catalogs:           [catalog_video() 的輸出, ...]
    total_sec:          主畫面總秒數
    photos:             [{"path": str, "desc": str}, ...] 新聞配圖（靜態照片，
                        會以 Ken Burns 緩慢推移呈現），沒有就不給
    notes:              額外備註（例如 SOT 原音段佔用了哪支影片的哪個區間，配對要避開）

    回傳 (plan, tokens)：
      plan = [{"path": str|None, "start": float, "dur": float, "why": str,
               "photo": str|None}, ...]
      依序鋪滿 total_sec；path=None 且 photo=None 為黑幕。
    """
    photos = photos or []
    vid_lines = []
    for vi, cat in enumerate(catalogs):
        vid_lines.append(f"影片V{vi+1}（總長 {cat['duration']}s）：")
        for seg in cat["segments"]:
            vid_lines.append(f"  {seg_line(seg)}")
    for pi, p in enumerate(photos):
        vid_lines.append(f"照片P{pi+1}（靜態新聞配圖，可指定任意秒數）：{p.get('desc', '')}")
    nar_lines = [
        f"旁白{ni+1}（{n['start']}s~{n['end']}s）：{n['desc']}"
        for ni, n in enumerate(narration_segments)
    ]

    # 向量相似度提示（文字向量，客觀參考值）：每段旁白最相近的素材段落 top-2
    sim_block = ""
    try:
        foot_entries = []   # (label, text)
        for vi, cat in enumerate(catalogs):
            for seg in cat["segments"]:
                foot_entries.append((f"V{vi+1}的{seg['start']}~{seg['end']}秒",
                                     f"{seg.get('description','')} {seg.get('subject','')}"))
        nar_texts = [n["desc"] for n in narration_segments]
        if foot_entries and nar_texts:
            embs = _embed_texts(nar_texts + [t for _, t in foot_entries])
            nar_embs, foot_embs = embs[:len(nar_texts)], embs[len(nar_texts):]
            sim_lines = []
            for ni, ne in enumerate(nar_embs):
                scored = sorted(
                    ((_cosine(ne, fe), foot_entries[fi][0]) for fi, fe in enumerate(foot_embs)),
                    reverse=True)[:2]
                sim_lines.append(f"旁白{ni+1} ↔ " + "、".join(
                    f"{lbl}({s:.2f})" for s, lbl in scored))
            sim_block = ("\n語意相似度參考（自動計算，數值越高畫面描述與旁白越相關，"
                         "僅供參考、仍以你的判斷為準）：\n" + "\n".join(sim_lines) + "\n")
    except Exception:
        sim_block = ""   # 向量服務不可用就略過，不擋配對

    prompt = (
        "你是新聞影音剪輯師。請把旁白各段配上最合適的畫面（影片片段或新聞照片），輸出一條完整時間軸。\n"
        "旁白是照著這批素材寫的（write to picture），若旁白段落的描述有註明建議影片段落"
        "（如「配V1的0~5秒」），優先照建議採用。\n\n"
        f"旁白分段（成片主畫面共 {total_sec} 秒）：\n" + "\n".join(nar_lines) + "\n\n"
        "可用素材（【】內是結構化標記：主體/鏡位/畫質/畫面文字/是否有原音人聲）：\n"
        + "\n".join(vid_lines) + "\n"
        + sim_block + "\n"
        "規則：\n"
        f"1. 時間軸總長必須正好 {total_sec} 秒，依序排列\n"
        "2. 影片片段標明用哪支影片、從第幾秒取、取多長；照片只標 P 編號跟秒數\n"
        "3. 畫面內容要呼應該時間點的旁白內容\n"
        "4. ⚠️ 事件主體優先：旁白在講事件經過（如車輛行駛、案發、救援）時，"
        "優先配「拍到事件主體」的片段——就算那段很短、需要重複使用也可以，"
        "重複主體畫面遠勝過空景或不相關畫面；若有照片拍到主體（新聞配圖常是事件現場截圖），"
        "主體動態片段不夠長時就接照片，不要拿空景硬撐\n"
        "5. ⚠️ 受訪/講話特寫（標記「有原音人聲」或描述含「受訪」「對鏡頭說話」）當配音底圖有嚴格限制："
        "只能配「警方表示/說明/提醒/呼籲」這種引述性旁白，且連續使用不要超過 12 秒；"
        "敘事性旁白（講案發經過）配受訪特寫會顯得畫面空洞，寧可用主體畫面重複或照片\n"
        "5-0. ⚠️ **成片開頭第一段絕不用受訪/講話特寫**——觀眾第一眼看到有人動嘴、"
        "聲音卻是旁白，會有強烈違和感。開場一律用事件現場/環境/主體動態畫面（或照片）；"
        "另外若額外備註提到稍後會插入受訪原音段，在原音段出現**之前**也盡量不要用"
        "同一位受訪者的講話畫面當底圖（同一張臉先無聲後有聲，觀眾會混亂）\n"
        "5-1. 善用鏡位與畫質標記：開場/交代環境用遠景，講主體細節用中景/特寫；"
        "標記「晃動」「模糊」「過暗」的段落畫質差，有其他選擇時盡量避開\n"
        "5-2. ⚠️ 標記「過場空景」的段落是沒有事件主體的空鏡（空椅子、會場佈置、"
        "空走廊等）——只有在旁白正好在交代場地/環境時才用，講事件經過或人物時"
        "絕對不要選它當畫面（會顯得像沒剪乾淨的過場）；寧可重複用有主體的片段或照片\n"
        "6. ⚠️ 相鄰兩個片段不可取用同一支影片的重疊區間（例如上一段用了 V1 的 1~11 秒，"
        "下一段就不要再用 V1 的 8~15 秒——觀眾會看到同樣畫面連播兩次）\n"
        "6-1. ⚠️ 連戲一致性：相鄰片段若出現同一人物，其外觀（眼鏡/服裝/姿態）與光線"
        "要連續，不要讓同一人「上一秒戴眼鏡下一秒沒戴」相鄰跳接；也避免相鄰兩段"
        "鏡位相同、構圖幾乎一樣的跳接（jump cut）——中間換別的畫面或換鏡位\n"
        "7. 完全找不到相關畫面的旁白段落：有照片先用照片，真的都沒有才用 \"black\" 補黑幕\n"
        "8. 節奏：單一片段 4~8 秒為佳（少於 3 秒太碎；超過 9 秒觀眾會膩——短影音"
        "每 5~7 秒要換一個視覺）；引述性旁白配受訪畫面可放寬到 12 秒；照片單段 3~6 秒\n"
        "9. 盡量避免從影片最開頭 0 秒取（常有黑幀/晃動），可從段落起點往後 1~2 秒取\n"
        + (f"\n額外備註：{notes}\n\n" if notes else "\n")
        + "只回傳 JSON（不含說明）：\n"
        '{"timeline": [\n'
        '  {"video": "V1", "from": 12.0, "dur": 8.0, "why": "嫌犯亮刀對應旁白案發"},\n'
        '  {"video": "P1", "dur": 6.0, "why": "網傳畫面截圖對應旁白描述"},\n'
        '  {"video": "black", "dur": 5.0, "why": "無對應畫面"}\n'
        "]}"
    )

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _azure_client().chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=2048,
    )
    result = json.loads(resp.choices[0].message.content)

    tokens = {}
    if resp.usage:
        tokens = {
            "prompt_tokens":     resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens":      resp.usage.total_tokens,
        }

    # ── 驗證與修正：檔案對應、範圍夾擠、總長補正 ────────────────────────────────
    plan: list[dict] = []
    acc = 0.0
    for entry in result.get("timeline", []):
        if acc >= total_sec - 0.05:
            break
        dur = float(entry.get("dur", 0) or 0)
        if dur <= 0.05:
            continue
        dur = min(dur, total_sec - acc)

        vid_id = str(entry.get("video", "black")).strip().upper()

        if vid_id.startswith("P"):
            try:
                pi = int(vid_id[1:]) - 1
                photo_path = photos[pi]["path"]
                plan.append({"path": None, "photo": photo_path, "start": 0.0,
                             "dur": round(dur, 2), "why": entry.get("why", "")})
                acc += dur
                continue
            except (ValueError, IndexError, KeyError):
                pass  # P 編號對不上 → 往下當黑幕處理

        if vid_id == "BLACK" or not vid_id.startswith("V"):
            plan.append({"path": None, "photo": None, "start": 0.0, "dur": dur,
                         "why": entry.get("why", "")})
            acc += dur
            continue

        try:
            vi = int(vid_id[1:]) - 1
            cat = catalogs[vi]
        except (ValueError, IndexError):
            plan.append({"path": None, "photo": None, "start": 0.0, "dur": dur,
                         "why": entry.get("why", "")})
            acc += dur
            continue

        start = max(0.0, float(entry.get("from", 0) or 0))
        avail = cat["duration"] - start
        if avail <= 0.05:
            plan.append({"path": None, "photo": None, "start": 0.0, "dur": dur,
                         "why": entry.get("why", "")})
            acc += dur
            continue
        dur = min(dur, avail)
        plan.append({"path": cat["path"], "photo": None, "start": round(start, 2),
                     "dur": round(dur, 2), "why": entry.get("why", "")})
        acc += dur

    if acc < total_sec - 0.05:
        # 時間軸不足：先試著延長最後一個影片片段（來源還有畫面就用），
        # 再用照片墊，最後才黑幕
        gap = total_sec - acc
        if plan and plan[-1].get("path"):
            last = plan[-1]
            cat = next((c for c in catalogs if str(c["path"]) == str(last["path"])), None)
            if cat:
                avail = cat["duration"] - (last["start"] + last["dur"])
                take = round(min(max(avail, 0.0), gap), 2)
                if take > 0.05:
                    last["dur"] = round(last["dur"] + take, 2)
                    acc += take
                    gap = total_sec - acc
        if gap > 0.05:
            filler_photo = photos[0]["path"] if photos else None
            plan.append({"path": None, "photo": filler_photo, "start": 0.0,
                         "dur": round(gap, 2),
                         "why": "時間軸不足，" + ("以新聞配圖補足" if filler_photo else "補黑幕")})

    # 重複片段去重：同一支影片被取用到重疊區間會讓觀眾看到同畫面連播兩次
    plan = _dedup_overlaps(plan, catalogs)

    # 碎片段合併：<2 秒的片段觀眾根本來不及看，直接併給前一段（延長前段時長）
    merged: list[dict] = []
    for e in plan:
        if merged and e["dur"] < 2.0:
            merged[-1]["dur"] = round(merged[-1]["dur"] + e["dur"], 2)
        elif not merged and e["dur"] < 2.0 and len(plan) > 1:
            e["_absorb_next"] = True   # 第一段太碎 → 標記讓下一段吸收
            merged.append(e)
        else:
            if merged and merged[-1].pop("_absorb_next", False):
                e = {**e, "dur": round(e["dur"] + merged[-1]["dur"], 2)}
                merged[-1] = e
                continue
            merged.append(e)
    plan = merged

    return plan, tokens


# ── 主入口 ───────────────────────────────────────────────────────────────────

def select_clip(
    video: Path,
    use_vision: bool = True,
    progress_cb=None,          # 可選 callback(layer: int, msg: str)
) -> dict:
    """
    三層篩選，回傳建議片段資訊。

    Returns dict:
      start_sec    建議起始秒數
      category     車禍 / 警匪槍戰 / 社會事件 / 其他
      description  事件說明（Layer 3 才有）
      confidence   high / medium / low
      method       heuristic / vision
      all_candidates 所有候選區段
    """
    video = Path(video)
    if not video.exists():
        raise ValueError(f"找不到路徑：{video}")
    if video.is_dir():
        raise ValueError(f"這是資料夾，不是影片檔案，請選擇資料夾裡的一支影片：{video}")
    TMP.mkdir(parents=True, exist_ok=True)

    cached = _cache_get("select", video)
    if cached and use_vision:
        if progress_cb:
            progress_cb(3, "使用快取結果（此影片先前分析過，零 API 花費）")
        if "duration" not in cached:  # 舊版快取沒存片長，補量一次（本地 ffprobe，免費）
            cached = {**cached, "duration": round(_duration(video), 1)}
        return {**cached, "tokens": {}}

    def _progress(layer: int, msg: str):
        if progress_cb:
            progress_cb(layer, msg)
        else:
            print(f"[Layer {layer}] {msg}")

    _progress(1, f"動態偵測：{video.name}")
    video_dur = round(_duration(video), 1)
    segments = get_motion_segments(video)
    audio = analyze_audio(video)
    is_cctv = _is_cctv_style(video, audio)
    _progress(1, (
        f"找到 {len(segments)} 個動態區段 | "
        f"音訊：{'有' if audio['has_audio'] else '無'} | "
        f"突波：{'有' if audio.get('has_spike') else '無'} | "
        f"監視器畫面：{'是' if is_cctv else '否'}"
    ))

    _progress(2, "規則分類...")
    candidates = classify_heuristic(segments, audio, is_cctv)
    if not candidates:
        return {
            "start_sec": 0.0, "category": "其他",
            "description": "無法偵測動態區段",
            "confidence": "low", "method": "heuristic",
            "duration": video_dur,
            "all_candidates": [],
        }

    top = candidates[0]
    _progress(2, (
        f"最佳候選：第 {top['start']:.0f}s ~ {top['end']:.0f}s | "
        f"初步分類：{top['preliminary_category']} ({top['preliminary_confidence']})"
    ))

    if not use_vision:
        return {
            "start_sec": round(top["start"], 1),
            "category": top["preliminary_category"],
            "description": "",
            "confidence": top["preliminary_confidence"],
            "method": "heuristic",
            "duration": video_dur,
            "all_candidates": candidates,
        }

    _progress(3, f"GPT-4o Vision 分析（從第 {top['start']:.0f}s 抽 5 張影格）...")
    result = classify_with_vision(video, top, top["preliminary_category"])
    result["method"] = "vision"
    result["duration"] = video_dur
    result["all_candidates"] = candidates
    # tokens 已內嵌在 result["tokens"]，由呼叫端讀取
    _cache_put("select", video, {k: v for k, v in result.items() if k != "tokens"})

    _progress(3, (
        f"分類：{result['category']} | "
        f"信心：{result['confidence']} | "
        f"建議起始：{result['start_sec']}s | "
        f"{result['description']}"
    ))

    return result


# ── CLI 入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python select_clip.py <影片路徑> [--no-vision]")
        sys.exit(1)

    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    video_path = Path(sys.argv[1])
    use_vision = "--no-vision" not in sys.argv

    result = select_clip(video_path, use_vision=use_vision)
    print("\n=== 選片結果 ===")
    # 移除 all_candidates 讓輸出簡潔
    display = {k: v for k, v in result.items() if k != "all_candidates"}
    print(json.dumps(display, ensure_ascii=False, indent=2))
