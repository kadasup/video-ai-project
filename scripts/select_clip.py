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
    st = video.stat()
    return f"{video}|{int(st.st_mtime)}|{st.st_size}"


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


def catalog_video(video: Path, max_segments: int = 10) -> dict:
    """
    幫一支影片建「內容目錄」：動態偵測切段，每段抽 2 張影格，
    一次 GPT 呼叫描述所有段落的畫面內容。

    回傳:
      {"path": str, "duration": float,
       "segments": [{"start": float, "end": float, "description": str}, ...],
       "tokens": {...}}
    """
    video = Path(video)

    # namespace 帶版本：切段粒度改過（catalog→catalog2），舊粗粒度快取自動失效
    cached = _cache_get("catalog2", video)
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
        "請描述每一段畫面拍到什麼（人事物與動作，具體、15 字內），"
        "只回傳 JSON（不含說明）：\n"
        '{"segments": [{"index": 1, "description": "..."}, ...]}'
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
        max_completion_tokens=512,
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

    desc_map = {d.get("index"): d.get("description", "") for d in result.get("segments", [])}
    for i, seg in enumerate(segments):
        seg["description"] = desc_map.get(i + 1, "（未取得描述）")

    for _, f in frames:
        try:
            f.unlink()
        except Exception:
            pass

    result = {"path": str(video), "duration": round(duration, 1),
              "segments": segments}
    _cache_put("catalog2", video, result)
    return {**result, "tokens": tokens}


# ── 旁白 ↔ 片段 智慧配對 ─────────────────────────────────────────────────────

def match_narration_to_clips(
    narration_segments: list[dict],
    catalogs: list[dict],
    total_sec: float,
) -> tuple[list[dict], dict]:
    """
    narration_segments: [{"start": 0, "end": 18, "desc": "..."}] 來自 generate_script
    catalogs:           [catalog_video() 的輸出, ...]
    total_sec:          主畫面總秒數

    回傳 (plan, tokens)：
      plan = [{"path": str|None, "start": float, "dur": float, "why": str}, ...]
      依序鋪滿 total_sec；path=None 為黑幕。
    """
    vid_lines = []
    for vi, cat in enumerate(catalogs):
        vid_lines.append(f"影片V{vi+1}（總長 {cat['duration']}s）：")
        for seg in cat["segments"]:
            vid_lines.append(
                f"  {seg['start']}s~{seg['end']}s：{seg.get('description', '')}"
            )
    nar_lines = [
        f"旁白{ni+1}（{n['start']}s~{n['end']}s）：{n['desc']}"
        for ni, n in enumerate(narration_segments)
    ]

    prompt = (
        "你是新聞影音剪輯師。請把旁白各段配上最合適的影片畫面，輸出一條完整時間軸。\n"
        "旁白是照著這批素材寫的（write to picture），若旁白段落的描述有註明建議影片段落"
        "（如「配V1的0~5秒」），優先照建議採用。\n\n"
        f"旁白分段（成片主畫面共 {total_sec} 秒）：\n" + "\n".join(nar_lines) + "\n\n"
        "可用素材片段：\n" + "\n".join(vid_lines) + "\n\n"
        "規則：\n"
        f"1. 時間軸總長必須正好 {total_sec} 秒，依序排列\n"
        "2. 每個片段標明用哪支影片、從第幾秒取、取多長\n"
        "3. 畫面內容要呼應該時間點的旁白內容；同一素材區段可重複使用，但盡量少重複\n"
        "4. 完全找不到相關畫面的旁白段落，用 \"black\" 補黑幕；"
        "但「畫面相關性普通」優於黑幕——寧可放大致相關的現場畫面，也不要黑\n"
        "5. 單一片段 5~15 秒為佳（少於 4 秒太碎、超過 20 秒太拖）\n"
        "6. 選畫面的優先序：有人物動作/事件發生的段落 > 靜態現場 > 空景；"
        "描述裡有具體人事物（如「消防員救援」「車輛翻覆」）的段落優先配給講到相同人事物的旁白\n"
        "7. 盡量避免從影片最開頭 0 秒取（常有黑幀/晃動），可從段落起點往後 1~2 秒取\n\n"
        "只回傳 JSON（不含說明）：\n"
        '{"timeline": [\n'
        '  {"video": "V1", "from": 12.0, "dur": 8.0, "why": "嫌犯亮刀對應旁白案發"},\n'
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
        if vid_id == "BLACK" or not vid_id.startswith("V"):
            plan.append({"path": None, "start": 0.0, "dur": dur,
                         "why": entry.get("why", "")})
            acc += dur
            continue

        try:
            vi = int(vid_id[1:]) - 1
            cat = catalogs[vi]
        except (ValueError, IndexError):
            plan.append({"path": None, "start": 0.0, "dur": dur,
                         "why": entry.get("why", "")})
            acc += dur
            continue

        start = max(0.0, float(entry.get("from", 0) or 0))
        avail = cat["duration"] - start
        if avail <= 0.05:
            plan.append({"path": None, "start": 0.0, "dur": dur,
                         "why": entry.get("why", "")})
            acc += dur
            continue
        dur = min(dur, avail)
        plan.append({"path": cat["path"], "start": round(start, 2),
                     "dur": round(dur, 2), "why": entry.get("why", "")})
        acc += dur

    if acc < total_sec - 0.05:
        plan.append({"path": None, "start": 0.0,
                     "dur": round(total_sec - acc, 2), "why": "時間軸不足補黑幕"})

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
