"""
社會新聞短影音產製 Web UI
用法：
  cd D:\\VideoAI\\scripts && python app.py
  瀏覽器開 http://localhost:5000
"""

import base64
import datetime
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

# ── Windows 主控台預設 cp950，遇到 ⚠ 等 emoji 的 print 會 UnicodeEncodeError，
#    導致產製中途（如「串接輸出」）整個 job 掛掉。這裡把標準輸出強制轉 UTF-8，
#    並用 backslashreplace 當保險，確保任何字元都不會再讓 print 崩潰。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

from flask import Flask, jsonify, render_template_string, request, send_file

# ── 啟動時自動讀取 D:\VideoAI\.env 的 API key ────────────────────────────────
_ENV_FILE = Path(r"D:\VideoAI\.env")
_LOG_FILE = Path(r"D:\VideoAI\logs\jobs.jsonl")
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_lock = threading.Lock()

def _append_log(record: dict):
    """將一筆 job 記錄 append 到 JSONL log 檔"""
    with _log_lock:
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

_FEEDBACK_FILE = Path(r"D:\VideoAI\logs\feedback.jsonl")
_fb_lock = threading.Lock()

def _append_feedback(kind: str, job_id: str, payload: dict):
    """隱性回饋閉環第 1 層：記錄「GPT 的決定 vs 人最後改成什麼」的結構化 diff。
    append-only（比照回饋資料唯讀慣例，只增不刪），之後彙整成 prompt 改進依據。
    記錄失敗絕不影響產製流程。"""
    rec = {"job_id": job_id, "kind": kind,
           "timestamp": datetime.datetime.now().isoformat(timespec="seconds")}
    rec.update(payload)
    try:
        with _fb_lock:
            with _FEEDBACK_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _read_logs(limit: int = 200) -> list[dict]:
    """讀取最近 limit 筆（最新在前）"""
    if not _LOG_FILE.exists():
        return []
    lines = _LOG_FILE.read_text(encoding="utf-8").splitlines()
    records = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            pass
        if len(records) >= limit:
            break
    return records

def _load_env():
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

# 引入 produce.py 的核心函式
sys.path.insert(0, str(Path(__file__).parent))
from produce import (
    BASE, OUTPUT, TMP, MAIN_SEC, TTS_VOICES,
    generate_script, shorten_script, factcheck_narration, extract_card,
    dedup_narration_vs_sots, generate_tts, write_ass,
    align_subtitles_to_boundaries, rescale_subtitles,
    align_segments_to_boundaries, rescale_segments,
    _probe_duration, _split_sentences, _build_subtitles,
    shift_subtitles, build_sot_subtitles, extract_audio_clip, concat_audio,
    _sec_to_ts,
    make_intro, make_main, make_main_plan, make_outro, concat,
)
from select_clip import (
    select_clip, catalog_video, match_narration_to_clips,
    video_fingerprint, near_duplicate, seg_line, describe_photo,
    _cache_get as _photodesc_cache_get,
)
import ltn_api
import speech
import translit

app = Flask(__name__)

VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.ts'}

def validate_video_path(path_str: str) -> str | None:
    """檢查影片路徑是否可用；回傳錯誤訊息字串，None 表示通過"""
    p = Path(path_str)
    if not p.exists():
        return f'找不到路徑：{path_str}'
    if p.is_dir():
        return f'這是資料夾，不是影片檔案，請選擇資料夾裡的一支影片：{path_str}'
    if p.suffix.lower() not in VIDEO_EXTS:
        return f'不支援的檔案格式（{p.suffix or "無副檔名"}）：{path_str}'
    return None

# ── 產製工作狀態 ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_busy = False
_job: dict = {'step': 0, 'msg': '', 'done': True, 'error': None,
              'filename': None, 'title': None}

# ── 分鏡確認 checkpoint（產製跑到配對完成後暫停、等使用者確認才進最後渲染）──────
_confirm_event = threading.Event()   # 主執行緒用它喚醒被暫停的產製執行緒
_confirm_action = ['go']             # 使用者的決定：'go'（渲染）/ 'cancel'（放棄）
_confirm_edits = [None]              # 分鏡站的人工換片段清單 [{index, path, start}]
_confirm_meta = [None]               # 分鏡站改過的開場標題/Hook/卡片移除 {'title','hook','removals'}
_script_dropcard = [False]           # 旁白稿站勾了「這支不用資訊卡」


def _remove_card_entries(plan: list, removals: list) -> tuple[list, int]:
    """
    分鏡站移除資訊卡（📊 開頭的段）：時長併給相鄰的非鎖定段（前鄰優先），
    時間軸總長不變 → 不需要重新配對。回傳 (新 plan, 移除數)。
    """
    out = [dict(e) for e in plan]
    removed = 0
    for i in sorted({int(x) for x in (removals or []) if str(x).lstrip('-').isdigit()},
                    reverse=True):
        if not (0 <= i < len(out)):
            continue
        if not str(out[i].get('why', '')).startswith('📊'):
            continue   # 只開放移除資訊卡（SOT 鎖死不能動）
        d = float(out[i].get('dur', 0) or 0)
        j = None
        if i - 1 >= 0 and not str(out[i - 1].get('why', '')).startswith(('🎤', '📊')):
            j = i - 1
        elif i + 1 < len(out) and not str(out[i + 1].get('why', '')).startswith(('🎤', '📊')):
            j = i + 1
        if j is None:
            continue   # 兩邊都是鎖定段（幾乎不可能），放著
        out[j]['dur'] = round(float(out[j].get('dur', 0)) + d, 2)
        if j == i + 1 and out[j].get('path'):   # 後鄰吸收＝取用起點往前移
            out[j]['start'] = round(max(0.0, float(out[j].get('start', 0)) - d), 2)
        out.pop(i)
        removed += 1
    return out, removed

# ── 快速預覽（分鏡站的低畫質草稿渲染）───────────────────────────────────────
_pending_ctx: dict = {}              # 分鏡確認暫停期間的渲染上下文（給預覽用）
_draft = {"running": False, "error": None, "file": None, "ts": 0}


def _apply_plan_edits(plan: list, edits: list, cat_paths: set) -> tuple[list, int]:
    """套用分鏡站的人工換片段（只換畫面來源與起始秒，時長/時間軸不動）。
    回傳 (新 plan, 套用筆數)；SOT/數據卡段鎖定不可換。"""
    out = [dict(e) for e in plan]
    applied = 0
    for ed in (edits or []):
        try:
            ei = int(ed.get('index'))
            npath = str(ed.get('path') or '')
            nstart = float(ed.get('start') or 0)
        except Exception:
            continue
        if not (0 <= ei < len(out)) or npath not in cat_paths:
            continue
        if str(out[ei].get('why', '')).startswith(('🎤', '📊')):
            continue
        out[ei] = {'path': npath, 'photo': None,
                   'start': round(nstart, 2), 'dur': out[ei]['dur'],
                   'why': '👤 人工指定片段'}
        applied += 1
    return out, applied


def _render_draft(edits: list, title_o: str = '', hook_o: str = '',
                  removals: list | None = None):
    """背景渲染低畫質預覽（480p/ultrafast），內容與正式版完全一致；
    title_o/hook_o 是分鏡站現改的開場標題/Hook（空=用原值）、removals=移除的資訊卡"""
    try:
        ctx = dict(_pending_ctx)
        if not ctx:
            raise RuntimeError('沒有等待中的分鏡（預覽只在分鏡確認時可用）')
        if title_o:
            ctx['title'] = title_o
        if hook_o:
            ctx['hook'] = hook_o
        plan2, _n = _apply_plan_edits(ctx['plan'], edits, ctx['cat_paths'])
        if removals:
            plan2, _n2 = _remove_card_entries(plan2, removals)
        clip, _info = make_main_plan(Path(ctx['tts']), Path(ctx['ass']), plan2,
                                     ctx['main_sec'], hook=ctx['hook'],
                                     straps=ctx['straps'], draft=True,
                                     title=ctx.get('title'))
        _draft.update({'file': str(clip), 'ts': time.time(), 'error': None})
    except Exception as e:
        _draft['error'] = str(e)
    finally:
        _draft['running'] = False
_preview: dict = {}                  # 暫停時給前端看的分鏡預覽（旁白/Hook/配對+縮圖/字幕）

# ── 旁白稿確認 checkpoint（腳本+查核完就暫停，TTS/配對前——改稿成本最低的時點）──
# 兩個 checkpoint 先後發生、絕不同時，共用同一組 event/action
_script_preview: dict = {}
_script_edit = [None]                # 使用者在旁白稿確認時改過的稿（None=沒改）


class _Cancelled(Exception):
    """使用者在分鏡確認 checkpoint 按了「取消」——正常中止，不是錯誤"""


def _frame_thumb_b64(video_path: str, t: float) -> str | None:
    """抽該片段一張代表影格、縮到 200 寬轉 base64（給分鏡預覽用，用完刪檔）"""
    from produce import ff
    out = TMP / f"_pv_thumb_{uuid.uuid4().hex[:8]}.jpg"   # 獨立檔名，供並行抽幀
    try:
        ff("-ss", str(max(0.0, t)), "-i", str(video_path),
           "-vframes", "1", "-vf", "scale=200:-1", "-q:v", "6", out)
        b = base64.b64encode(out.read_bytes()).decode()
        return b
    except Exception:
        return None
    finally:
        try:
            out.unlink()
        except Exception:
            pass


def _photo_thumb_b64(photo_path: str) -> str | None:
    """照片縮圖 base64（分鏡預覽用）"""
    from produce import ff
    out = TMP / f"_pv_thumb_{uuid.uuid4().hex[:8]}.jpg"
    try:
        ff("-i", str(photo_path), "-vframes", "1", "-vf", "scale=200:-1", "-q:v", "6", out)
        return base64.b64encode(out.read_bytes()).decode()
    except Exception:
        return None
    finally:
        try:
            out.unlink()
        except Exception:
            pass


def _build_preview(script: dict, subtitles: list, plan_rows: list,
                   factcheck: list | None = None,
                   translit_results: list | None = None,
                   catalogs: list | None = None) -> dict:
    """組裝分鏡預覽 payload：旁白、Hook、hashtags、字幕分段、每段配對＋代表縮圖；
    另附素材庫全部片段清單（options），供分鏡站「換畫面」下拉選用"""
    # 縮圖並行抽幀（原本逐張串行，7 段要 ~5 秒；並行 ~1 秒）
    from concurrent.futures import ThreadPoolExecutor

    def _one_thumb(e):
        if e.get('path'):
            return _frame_thumb_b64(e['path'], e.get('start', 0))
        if e.get('photo'):
            return _photo_thumb_b64(e['photo'])
        return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        thumbs = list(ex.map(_one_thumb, plan_rows or []))

    segs = []
    for e, thumb in zip(plan_rows or [], thumbs):
        if e.get('path'):
            label = Path(e['path']).name
        elif e.get('photo'):
            label = f"📷 {Path(e['photo']).name}（照片）"
        else:
            label = '（黑幕）'
        segs.append({
            'video': label,
            'start': e.get('start', 0),
            'dur': e.get('dur', 0),
            'why': e.get('why', ''),
            'thumb': thumb,
            # SOT 跟資訊卡的時長跟音軌/動畫鎖死，不能換；但資訊卡可以「移除」
            'editable': not str(e.get('why', '')).startswith(('🎤', '📊')),
            'removable': str(e.get('why', '')).startswith('📊'),
        })
    options = []
    for cat in (catalogs or []):
        vname = Path(str(cat['path'])).name
        for s in cat.get('segments', []):
            marks = [m for m in (s.get('subject'), s.get('shot')) if m]
            options.append({
                'path': str(cat['path']), 'name': vname,
                'start': s.get('start', 0), 'end': s.get('end', 0),
                'desc': (('【' + '|'.join(marks) + '】') if marks else '')
                        + (s.get('description') or '')[:42],
            })
    return {
        'title': script.get('title', ''),
        'hook': script.get('hook', ''),
        'hashtags': script.get('hashtags', []),
        'narration': script.get('narration', ''),
        'subtitles': [s.get('text', '') for s in (subtitles or [])],
        'segments': segs,
        'options': options,
        'factcheck': factcheck or [],
        'translit': translit_results or [],
    }

def _split_gpt_segments(segs: list, k: int) -> tuple[list, list]:
    """把 GPT 的 segments（含 sentences 計數）在第 k 句處切成前後兩批（SOT 插入用）"""
    a, b, acc = [], [], 0
    for s in segs:
        n = max(1, int(s.get('sentences', 1) or 1))
        if acc >= k:
            b.append(s)
        elif acc + n <= k:
            a.append(s)
        else:
            a.append({**s, 'sentences': k - acc})
            b.append({**s, 'sentences': acc + n - k})
        acc += n
    return a, b


def _merge_tiny_plan_entries(plan: list) -> list:
    """
    SOT 切點會把片段切出 <2 秒的碎片，觀眾來不及看，併給相鄰段。
    SOT 段（why 以 🎤 開頭）的時長跟音軌鎖死，絕不能被增減，只當「跳過」處理。
    """
    def is_sot(e):
        # 🎤 SOT 時長跟音軌鎖死；📊 數據卡時長跟渲染出的動畫鎖死——都不能被增減
        return str(e.get('why', '')).startswith(('🎤', '📊'))

    out = list(plan)
    changed = True
    while changed:
        changed = False
        for i, e in enumerate(out):
            if is_sot(e) or float(e.get('dur', 0)) >= 2.0:
                continue
            d = float(e.get('dur', 0))
            nxt = out[i + 1] if i + 1 < len(out) else None
            prv = out[i - 1] if i > 0 else None
            if nxt is not None and not is_sot(nxt):
                nxt['dur'] = round(float(nxt['dur']) + d, 2)
                if nxt.get('path'):
                    nxt['start'] = round(max(0.0, float(nxt.get('start', 0)) - d), 2)
            elif prv is not None and not is_sot(prv):
                prv['dur'] = round(float(prv['dur']) + d, 2)
            else:
                continue   # 兩邊都是 SOT（幾乎不可能），放著
            out.pop(i)
            changed = True
            break
    return out


FACE_MODEL = BASE / "assets" / "face_detection_yunet.onnx"


def _sot_face_factor(video_path, start, dur):
    """
    SOT 臉部置中：抓受訪片段中點一格，用 YuNet 偵測最大的臉，算出讓臉落在
    9:16 直式裁切正中的水平偏移 factor（0~1）。缺模型/抓不到臉→回 None，
    呼叫端退回正中 0.5（比粗略的左/右裁切保險）。
    """
    if not FACE_MODEL.exists():
        return None
    try:
        import cv2
        from produce import ff
        frame = TMP / f"_sot_face_{uuid.uuid4().hex[:8]}.jpg"
        t = float(start or 0) + float(dur or 0) / 2
        ff("-ss", f"{t}", "-i", str(video_path), "-frames:v", "1", "-q:v", "3", frame)
        img = cv2.imread(str(frame))
        try:
            frame.unlink(missing_ok=True)
        except Exception:
            pass
        if img is None:
            return None
        h, w = img.shape[:2]
        det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), score_threshold=0.6)
        det.setInputSize((w, h))
        _n, faces = det.detect(img)
        if faces is None or len(faces) == 0:
            return None
        f = max(faces, key=lambda b: float(b[2]) * float(b[3]))   # 面積最大的臉
        face_cx = float(f[0]) + float(f[2]) / 2.0                 # 臉中心 x（像素）
        cw = h * 9.0 / 16.0                                       # 直式裁切窗寬（保留全高）
        if w <= cw:
            return 0.5                                            # 來源已接近直式
        return min(1.0, max(0.0, (face_cx - cw / 2.0) / (w - cw)))
    except Exception:
        return None


def _enrich_subject_pos(plan: list, catalogs: list) -> None:
    """
    給每個影片片段查它取用的區間主體在畫面哪一側（左/中/右），
    寫進 entry['subject_pos'] 供直式裁切偏移。就地修改 plan。
    SOT 段（🎤）另外走臉部偵測，把受訪者的臉抓到畫面正中（entry['crop_factor']）。
    """
    seg_map = {}   # path -> [(start, end, subject_pos)]
    for cat in catalogs:
        seg_map[str(cat['path'])] = [
            (s['start'], s['end'], s.get('subject_pos', '滿版')) for s in cat['segments']]
    for e in plan:
        if not e.get('path'):
            continue
        if str(e.get('why', '')).startswith('🎤'):
            # SOT 受訪段：臉部置中優先，抓不到臉就正中（0.5）
            f = _sot_face_factor(e['path'], e.get('start', 0), e.get('dur', 0))
            e['crop_factor'] = f if f is not None else 0.5
            continue
        segs = seg_map.get(str(e['path']))
        if not segs:
            continue
        mid = float(e.get('start', 0) or 0) + float(e.get('dur', 0) or 0) / 2
        # 取涵蓋中點的段落；找不到就取最接近的
        pos = next((sp for st, en, sp in segs if st <= mid <= en), None)
        if pos is None and segs:
            pos = min(segs, key=lambda x: abs((x[0] + x[1]) / 2 - mid))[2]
        e['subject_pos'] = pos or '滿版'


def _split_plan_at(plan: list, t: float) -> tuple[list, list]:
    """把配對時間軸在第 t 秒處切開（跨界的片段拆成兩半），供插入 SOT 段"""
    a, b, acc = [], [], 0.0
    for e in plan:
        d = float(e.get('dur', 0) or 0)
        if acc >= t - 0.01:
            b.append(e)
        elif acc + d <= t + 0.01:
            a.append(e)
        else:
            take = round(t - acc, 2)
            e2 = {**e, 'dur': round(d - take, 2)}
            if e.get('path'):
                e2['start'] = round(float(e.get('start', 0) or 0) + take, 2)
            a.append({**e, 'dur': take})
            b.append(e2)
        acc += d
    return a, b


_TALK_KEYWORDS = ('受訪', '說話', '發言', '講話', '對鏡頭', '口型', '受访')

def _coverage_warning(plan: list, catalogs: list) -> str | None:
    """
    素材涵蓋度預警：算出成片裡「受訪特寫當底」「照片」「黑幕」各佔幾秒，
    佔比過高就回警告字串——讓「素材撐不起旁白」在確認分鏡時就被看見，
    而不是整支渲染完才發現畫面空洞。
    """
    desc_map = {}   # path -> [(start, end, is_talk)]
    for cat in catalogs:
        desc_map[str(cat['path'])] = [
            (s['start'], s['end'],
             bool(s.get('has_speech')) or any(k in s.get('description', '')
                                              for k in _TALK_KEYWORDS))
            for s in cat['segments']]

    total = talk = photo = black = 0.0
    for e in plan:
        dur = float(e.get('dur', 0) or 0)
        total += dur
        if e.get('photo'):
            photo += dur
        elif not e.get('path'):
            black += dur
        else:
            segs = desc_map.get(str(e['path']), [])
            s0 = float(e.get('start', 0) or 0)
            s1 = s0 + dur
            talk_overlap = sum(
                max(0.0, min(s1, seg_end) - max(s0, seg_start))
                for seg_start, seg_end, is_talk in segs if is_talk)
            talk += talk_overlap
    if total <= 0:
        return None

    parts = []
    if talk / total > 0.4:
        parts.append(f"受訪特寫畫面佔了 {talk:.0f} 秒（{talk/total:.0%}）——"
                     "閉麥的講話臉當配音底圖太久會顯得空洞，建議補事件現場素材")
    if black > 0.05:
        parts.append(f"黑幕 {black:.0f} 秒")
    if photo / total > 0.5:
        parts.append(f"靜態照片佔 {photo:.0f} 秒（{photo/total:.0%}），動態素材偏少")
    return ("⚠️ 素材涵蓋度提醒：" + "；".join(parts)) if parts else None


def _stop_reasons(factcheck, translit, plan, catalogs) -> list[str]:
    """
    智慧停站判斷：分鏡站只在「有理由讓人看」時才停下來，其餘自動渲染進成片庫。
    回傳「該停的理由」清單（空＝沒事、可自動過）。判準都用結構化訊號，不靠警示字串：
      ①事實查核有硬矛盾（非省略）②譯名與音譯表不符 ③素材涵蓋度偏低 ④成片含黑幕段。
    保守原則：任一有問題就停，寧可多停也不讓有瑕疵的成片無聲無息跑完。
    """
    reasons = []
    fc = factcheck or []
    bad = [f for f in fc
           if not ((f.get('type') == '省略')
                   or (f.get('type') is None and f.get('severity') == 'low'))]
    if bad:
        reasons.append(f'事實查核 {len(bad)} 處與原稿牴觸')
    mism = [t for t in (translit or []) if t.get('status') == 'mismatch']
    if mism:
        reasons.append(f'譯名 {len(mism)} 處與音譯表不符')
    try:
        if _coverage_warning(plan or [], catalogs or []):
            reasons.append('素材涵蓋度偏低（黑幕/受訪佔比過高）')
    except Exception:
        pass   # 涵蓋度計算失敗不該擋停站判斷（黑幕段另有獨立判準）
    if any((not s.get('path')) and (not s.get('photo')) for s in (plan or [])):
        reasons.append('有配不到畫面的黑幕段')
    if any(c.get('fallback') for c in (catalogs or [])):
        reasons.append('有素材自動辨識失敗（內容安全過濾器誤擋等），以通用描述納入，請確認畫面')
    return reasons


def _fallback_catalog(video: Path) -> dict:
    """
    catalog_video 失敗時的備援目錄（常見主因：Azure 內容安全過濾器把災害/事故畫面
    誤判成違規而 400）。不要靜默丟掉整支素材——把它切成數段通用「現場實況畫面」，
    讓配對仍可使用（比只剩一顆講話頭好太多），並在成片流程標記 fallback 供停站提醒。
    """
    try:
        dur = float(_probe_duration(video))
    except Exception:
        dur = 0.0
    if dur <= 0:
        dur = 8.0
    segs, t = [], 0.0
    while t < dur - 0.5 and len(segs) < 10:
        end = round(min(dur, t + 5.0), 2)
        segs.append({"start": round(t, 2), "end": end, "subject_pos": "滿版",
                     "description": "現場實況畫面（自動辨識被內容安全系統擋下、未詳細描述，請於分鏡站確認）"})
        t = end
    return {"path": str(video), "duration": round(dur, 2),
            "segments": segs, "tokens": {}, "fallback": True}


def _fallback_sot(transcripts: list, catalogs: list, narration: str):
    """
    GPT 沒排 SOT、但素材有夠份量的受訪逐字稿時，自動補一段（保險，只在完全沒 SOT 時用）。
    從連續逐字稿段落挑一段 6~14 秒、字數最多（最有內容）的當 SOT，讓「一顆講話頭」的
    新聞也一定用到當事人原音、而不是讓他無聲動嘴。抓不到合適段落回 None。
    after_sentence 先粗抓（插在旁白引導說話那句後），最終由後續去重步驟對齊。
    """
    def _zh_len(s):
        return len(re.sub(r"[^一-鿿0-9A-Za-z]", "", s or ""))
    best = None
    for vi, tr in enumerate(transcripts):
        segs = tr.get('segments') or []
        for i in range(len(segs)):
            j = i
            while j < len(segs) and (segs[j]['end'] - segs[i]['start']) <= 14.0:
                dur = segs[j]['end'] - segs[i]['start']
                if dur >= 6.0:
                    text = "".join(s['text'] for s in segs[i:j + 1])
                    if _zh_len(text) >= 12 and (best is None or len(text) > best['sc']):
                        best = {'vi': vi, 'st': segs[i]['start'],
                                'en': segs[j]['end'], 'text': text, 'sc': len(text)}
                j += 1
    if not best:
        return None
    sents = [s for s in re.split(r'(?<=[。！？])', narration or '') if s.strip()]
    intro_kw = ('表示', '說明', '指出', '呼籲', '提醒', '坦言', '強調', '回應',
                '受訪', '到場', '說')
    after = next((idx for idx, s in enumerate(sents, 1)
                  if any(k in s for k in intro_kw)), 0)
    if not after:
        after = max(1, round(len(sents) * 0.4))
    vi = best['vi']
    return {'path': catalogs[vi]['path'], 'start': round(best['st'], 2),
            'dur': round(best['en'] - best['st'], 2),
            'display': best['text'].strip(), 'after_sentence': after,
            'label': f"V{vi + 1}", 'spk_name': '', 'spk_title': '', 'speaker': '',
            'auto': True}


# ── 智慧分析工作狀態 ───────────────────────────────────────────────────────────
_alock = threading.Lock()
_abusy = False
_analysis: dict = {
    'done': True, 'error': None,
    'layer': 0, 'msg': '',
    'result': None,   # select_clip 回傳的 dict
}

# 順序刻意「先分析素材、再寫腳本」（write to picture）：
# 旁白要貼著現有畫面寫，配對才搭得起來
STEPS = ['', '分析影片內容', '生成播報腳本', '生成旁白音訊', '寫字幕檔',
         # 步驟6保留位置：冷開場改版後片頭卡取消（標題改疊主畫面），此步瞬時完成
         '旁白配對畫面',
         '冷開場（無片頭卡）', '組裝主畫面', '組裝片尾（2.8秒）', '串接輸出']


def run_job(article: str, videos: list[dict], fname: str | None, voice_key: str = "hsiaochen",
            checkpoint_mode: str = "smart", photos: list[str] | None = None,
            low_quality: bool = True):
    """videos: [{"path": "...", "start": 12.0}, ...]，依序銜接補滿 MAIN_SEC 秒
    photos: 新聞配圖本機路徑（匯入時下載的文章主圖），配不到動態畫面的旁白段
            會用「照片＋Ken Burns 推移」取代黑幕/空洞畫面。
    checkpoint_mode：
      'smart'（預設）＝跳過腳本站；分鏡站只在有理由時才停（見 _stop_reasons），否則自動渲染
      'always'＝每支都停腳本站＋分鏡站（逐支把關）；'off'＝全自動一路跑完不停站。
    low_quality=True（POC 低畫質模式，預設）：正式渲染也走 480p/ultrafast＋480p 片尾，
    完整流程照舊（進成片庫、metadata、回饋閉環），只是畫質低、渲染快；上線時關掉恢復 1080p。"""
    global _busy, _job
    voice = TTS_VOICES.get(voice_key, TTS_VOICES["hsiaochen"])
    # 讓前端能還原「這個工作實際用的輸入」（重跑時產製頁清單要顯示重跑的素材，
    # 不是使用者上次自己選的清單）
    _confirm_meta[0] = None   # 清掉上一輪殘留的標題/Hook 修改
    _job['input_videos'] = [v['path'] for v in videos]
    _job['input_photos'] = list(photos or [])
    _job['input_article'] = article

    t0 = time.time()
    job_id = str(uuid.uuid4())[:8]
    model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    token_usage: dict = {}
    output_name: str | None = None
    err_msg: str | None = None
    length_info: dict = {}
    step_durations: dict = {}
    script: dict = {}
    _last_step_t = [t0]

    def p(step: int):
        now = time.time()
        prev = _job['step']
        if prev > 0:
            step_durations[STEPS[prev]] = round(now - _last_step_t[0], 1)
        _last_step_t[0] = now
        _job['step'] = step
        _job['msg'] = STEPS[step]
        _job['step_started_at'] = now
        _job['step_durations'] = dict(step_durations)

    def _add_tokens(t: dict):
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            token_usage[k] = token_usage.get(k, 0) + t.get(k, 0)
        token_usage["frames_sent"] = token_usage.get("frames_sent", 0) + t.get("frames_sent", 0)

    try:
        TMP.mkdir(parents=True, exist_ok=True)
        OUTPUT.mkdir(parents=True, exist_ok=True)

        tts_path = TMP / "_narration_full.m4a"   # 旁白（+SOT 原音）合併後的完整音軌
        ass_path = TMP / "_subtitles.ass"

        # ── 步驟1：先分析素材（近重複偵測→畫面目錄→受訪原音轉譯）─────────────────
        p(1)

        # 近重複偵測：記者常同場景連拍多支，dHash 指紋聚類後同組只留最長的一支
        # （省 API 分析錢，也避免配對重複選到同畫面）。長度差 >20% 不視為重複
        # ——受訪 take1/take2 畫面像但內容不同，要保守。
        dedup_skipped = []
        if len(videos) > 1:
            _job['msg'] = f"{STEPS[1]}（近重複素材偵測中）"
            metas = []
            for v in videos:
                try:
                    d = _probe_duration(Path(v['path']))
                    fp = video_fingerprint(Path(v['path']), d)
                except Exception:
                    d, fp = 0, [None, None, None]
                metas.append({'v': v, 'dur': d, 'fp': fp})
            kept: list[dict] = []
            for m in sorted(metas, key=lambda x: -x['dur']):   # 長的優先留
                if any(near_duplicate(m['fp'], k['fp'], m['dur'], k['dur']) for k in kept):
                    dedup_skipped.append(Path(m['v']['path']).name)
                else:
                    kept.append(m)
            if dedup_skipped:
                keep_paths = {id(k['v']) for k in kept}
                videos = [v for v in videos if id(v) in keep_paths]   # 保留原相對順序
                _job['warning'] = (f"已略過 {len(dedup_skipped)} 支近重複素材："
                                   + "、".join(dedup_skipped))

        catalogs = []
        transcripts = []   # 與 catalogs 同 index：該支影片的受訪逐字稿（無人聲為空）
        if videos:
            for i, v in enumerate(videos):
                _job['msg'] = f"{STEPS[1]}（{i+1}/{len(videos)}）"
                try:
                    cat = catalog_video(Path(v['path']))
                    _add_tokens(cat.get('tokens', {}))
                except Exception as e:
                    # 自動辨識失敗（常見：內容安全過濾器誤擋災害/事故畫面 → 400 content_policy）。
                    # 不要靜默丟掉整支！用備援目錄讓素材仍可配對，並清楚警示編輯：什麼原因、哪一支。
                    cat = _fallback_catalog(Path(v['path']))
                    fname = Path(v['path']).name
                    if 'content_policy' in str(e) or 'content safety' in str(e):
                        msg = (f"⚠️ 素材「{fname}」被 Azure 內容安全系統擋下"
                               "（原因 content_policy_violation：畫面被判為疑似不當內容，"
                               "災害／事故／血腥感畫面常被誤判），因此無法自動辨識內容。"
                               "已以通用『現場實況畫面』納入配對，請務必到分鏡站確認這支畫面。")
                    else:
                        msg = (f"⚠️ 素材「{fname}」自動辨識失敗（原因：{str(e)[:80]}），"
                               "已以通用『現場實況畫面』納入配對，請到分鏡站確認這支畫面。")
                    _job['warning'] = ((_job.get('warning') + '\n' + msg)
                                       if _job.get('warning') else msg)
                _job['msg'] = f"{STEPS[1]}（{i+1}/{len(videos)}，原音轉譯中）"
                try:
                    tr = speech.transcribe(Path(v['path']))
                except Exception:
                    tr = {"segments": [], "full_text": ""}
                catalogs.append(cat)
                transcripts.append(tr)

        # 用實際轉譯結果標註每個畫面段落「有沒有原音人聲」（給配對與涵蓋度判斷用，
        # 比靠描述文字猜「受訪」可靠）
        for cat, tr in zip(catalogs, transcripts):
            spans = [(s['start'], s['end']) for s in tr.get('segments', [])]
            for seg in cat['segments']:
                ov = sum(max(0.0, min(seg['end'], e) - max(seg['start'], st))
                         for st, e in spans)
                seg['has_speech'] = ov > (seg['end'] - seg['start']) * 0.5

        # 素材畫面清單 → 給 GPT 貼著畫面寫稿（含結構化標記）
        footage_notes = None
        if catalogs:
            lines = []
            for vi, cat in enumerate(catalogs):
                lines.append(f"影片V{vi+1}（{Path(cat['path']).name}，總長 {cat['duration']} 秒）：")
                for seg in cat['segments']:
                    lines.append(f"  {seg_line(seg)}")
            footage_notes = "\n".join(lines)

        # 受訪逐字稿 → 給 GPT 安排 SOT（受訪原音段）
        interview_notes = None
        iv_lines = []
        for vi, tr in enumerate(transcripts):
            if not tr.get('segments'):
                continue
            iv_lines.append(f"影片V{vi+1} 的原音逐字稿：")
            for s in tr['segments']:
                iv_lines.append(f"  [{s['start']}s~{s['end']}s] {s['text']}")
        if iv_lines:
            interview_notes = "\n".join(iv_lines)

        p(2)
        script, script_tokens = generate_script(article, footage_notes, interview_notes)
        _add_tokens(script_tokens)
        _job['title'] = script['title']
        _job['hook'] = script.get('hook', '')
        _job['hashtags'] = script.get('hashtags', [])

        # ── SOT 解析：把 GPT 選的原音引句對回逐字稿時間軸（最多 2 段）───────────────
        sots = []
        for s0 in (script.get('sots') or [])[:2]:
            try:
                vi = int(str(s0.get('video', '')).strip().upper().lstrip('V')) - 1
                if not (0 <= vi < len(transcripts)):
                    continue
                span = speech.resolve_quote_span(transcripts[vi], s0.get('quote', ''))
                if not span:
                    continue
                st, en, _spansegs = span
                dur_s = round(en - st, 2)
                if dur_s > 16.0 or dur_s < 3.0:
                    continue   # 超過 16 秒太拖、不足 3 秒不成句，寧缺勿濫
                spk_name = (s0.get('speaker_name') or '').strip()
                spk_title = (s0.get('speaker_title') or '').strip()
                cand = {'path': catalogs[vi]['path'], 'start': st, 'dur': dur_s,
                        'display': (s0.get('display') or s0.get('quote') or '').strip(),
                        'after_sentence': int(s0.get('after_sentence', 0) or 0),
                        'label': f"V{vi+1}",
                        # 受訪者名條：GPT 只在能明確判斷發言者時才給（掛錯人是事故）
                        'spk_name': spk_name, 'spk_title': spk_title,
                        'speaker': ('｜'.join(x for x in (spk_name, spk_title) if x))}
                # 第二段防呆：同影片區間重疊（GPT 選了重複內容）或合計超過 22 秒就不收
                if any(s['path'] == cand['path']
                       and st < s['start'] + s['dur'] and s['start'] < en
                       for s in sots):
                    continue
                if sum(s['dur'] for s in sots) + dur_s > 22.0:
                    continue
                sots.append(cand)
            except Exception:
                continue
        sots.sort(key=lambda s: s['after_sentence'])
        # SOT 自動補排：GPT 沒排（隨機漏排）但素材有夠份量的受訪逐字稿 → 程式補一段，
        # 不讓「一顆講話頭」的新聞白白浪費當事人原音。插入點/縮稿由下游自動處理。
        if not sots:
            fb = _fallback_sot(transcripts, catalogs, script['narration'])
            if fb:
                sots.append(fb)
                _job['msg'] = f"{STEPS[2]}（GPT 未排原音，自動補排 SOT {fb['dur']:.0f} 秒）"
        _job['sot'] = ([{'label': s['label'], 'dur': s['dur'], 'display': s['display'],
                         'speaker': s.get('speaker', '')}
                        for s in sots] or None)

        # 長度保險 A：GPT 沒守字數上限就退件縮寫（有 SOT 時額度要扣掉原音秒數）
        sot_total = sum(s['dur'] for s in sots)
        nar_budget_sec = MAIN_SEC - sot_total
        if len(script['narration']) > int(nar_budget_sec * 4.6):
            _job['msg'] = f"{STEPS[2]}（旁白 {len(script['narration'])} 字超限，縮寫中）"
            try:
                script, sh_tokens = shorten_script(script, int(nar_budget_sec * 4.4))
                _add_tokens(sh_tokens)
                _job['title'] = script['title']
                _job['hook'] = script.get('hook', _job.get('hook', ''))
                _job['hashtags'] = script.get('hashtags', _job.get('hashtags', []))
            except Exception:
                pass  # 縮寫失敗就用原稿，交給長度保險 B
            # 縮寫後句數會變（GPT 被要求同步調整 after_sentence）→ 以縮寫後的值為準
            if sots and script.get('sots'):
                by_disp = {(x.get('display') or x.get('quote') or '').strip(): x
                           for x in script['sots']}
                for s in sots:
                    x = by_disp.get(s['display'])
                    if x:
                        try:
                            s['after_sentence'] = int(x.get('after_sentence') or s['after_sentence'])
                        except Exception:
                            pass
                sots.sort(key=lambda s: s['after_sentence'])

        # SOT 內容去重：旁白不要先把原音要講的講一遍（實測 GPT 寫稿常犯——
        # 旁白講完取消班次數字、原音又講同樣的事，觀眾聽兩遍）
        if sots:
            _job['msg'] = f"{STEPS[2]}（檢查旁白與原音重複）"
            try:
                new_nar, new_afters, dd_tokens = dedup_narration_vs_sots(
                    script['narration'], [s['display'] for s in sots])
                _add_tokens(dd_tokens)
                if new_nar != script['narration']:
                    script['narration'] = new_nar
                    if len(new_afters) == len(sots):
                        for s, k in zip(sots, new_afters):
                            s['after_sentence'] = k
                        sots.sort(key=lambda s: s['after_sentence'])
            except Exception:
                pass   # 去重是品質加分項，失敗用原稿

        # 資訊卡補判＋事實查核：兩個 GPT 呼叫互相獨立（旁白已定稿），並行發出省等待。
        # generate_script 給了 stat 就直接當數據卡；沒給才判四型（時序/警示/圖表不需要數字）
        _job['msg'] = f"{STEPS[2]}（事實查核中）"
        from concurrent.futures import ThreadPoolExecutor
        if isinstance(script.get('stat'), dict) and str(script['stat'].get('value', '')).strip():
            script['card'] = {**script['stat'], 'type': 'stat'}
        need_card = not script.get('card')
        with ThreadPoolExecutor(max_workers=2) as _ex:
            fut_fc = _ex.submit(factcheck_narration, article, script['narration'])
            fut_cd = (_ex.submit(extract_card, script['narration'])
                      if need_card else None)
            try:
                fc_issues, fc_translits, fc_tokens = fut_fc.result()
                _add_tokens(fc_tokens)
            except Exception:
                fc_issues, fc_translits = [], []
            if fut_cd is not None:
                try:
                    cd, cd_tokens = fut_cd.result()
                    _add_tokens(cd_tokens)
                    if cd:
                        script['card'] = cd
                except Exception:
                    pass
        _job['factcheck'] = fc_issues
        if fc_issues:
            highs = [i for i in fc_issues if i.get('severity') == 'high']
            fc_msg = (f"⚠️ 事實查核發現 {len(fc_issues)} 處與原稿不符"
                      + (f"（其中 {len(highs)} 處為數字/人名/地名等硬事實）" if highs else "")
                      + "，請在分鏡確認時核對旁白")
            _job['warning'] = ((_job.get('warning') + '\n' + fc_msg)
                               if _job.get('warning') else fc_msg)

        # 譯名核實：旁白裡的外國譯名 vs 報社音譯總表（同事維護的 4600 筆 Google Sheet）
        translit_results = []
        if fc_translits:
            try:
                translit_results = translit.check_names(fc_translits)
            except Exception:
                translit_results = []
        _job['translit'] = translit_results
        mismatches = [t for t in translit_results if t.get('status') == 'mismatch']
        if mismatches:
            tr_msg = ("⚠️ 譯名核實：" + "；".join(
                f"「{t['chinese']}」報社標準譯名是「{t['expected']}」" for t in mismatches))
            _job['warning'] = ((_job.get('warning') + '\n' + tr_msg)
                               if _job.get('warning') else tr_msg)

        # ── Checkpoint A：旁白稿確認（TTS/配對前先讓人看稿＋查核結果，改稿成本最低）──
        # 借鏡中央社流程：切角/旁白稿在早期就人工把關，稿不對後面全白跑
        # 只有 'always'（每支都停）才停腳本站；'smart' 一律跳過（查核結果會在分鏡站呈現）
        if checkpoint_mode == 'always':
            global _script_preview
            _script_preview = {
                'title': script.get('title', ''), 'hook': script.get('hook', ''),
                'hashtags': script.get('hashtags', []),
                'narration': script['narration'],
                'sots': [{'label': s['label'], 'dur': s['dur'],
                          'display': s['display'],
                          'speaker': s.get('speaker', '')} for s in sots],
                'card': script.get('card') or None,
                'highlights': script.get('highlights') or [],
                'factcheck': _job.get('factcheck') or [],
                'translit': _job.get('translit') or [],
            }
            _script_edit[0] = None
            _confirm_action[0] = 'go'
            _confirm_event.clear()
            _job['awaiting_script'] = True
            _job['msg'] = '⏸ 等待旁白稿確認（配音前）'
            got = _confirm_event.wait(timeout=1800)   # 最多等 30 分鐘，逾時視同取消
            _job['awaiting_script'] = False
            _script_preview = {}
            if (not got) or _confirm_action[0] == 'cancel':
                raise _Cancelled()
            if _script_edit[0]:   # 使用者改過稿 → 後面全部用改過的版本
                if _script_edit[0] != script.get('narration'):
                    _append_feedback('narration_edit', job_id, {
                        'title': script.get('title', ''),
                        'before': script.get('narration', ''),
                        'after': _script_edit[0]})
                script['narration'] = _script_edit[0]
            if _script_dropcard[0]:   # 勾了「這支不用資訊卡」
                if script.get('card'):
                    _append_feedback('card_dropped_at_script', job_id, {
                        'title': script.get('title', ''),
                        'card': script.get('card')})
                script['card'] = None
                script['stat'] = None
                _script_dropcard[0] = False

        # ── 依 SOT 插入點把旁白拆成交錯時間軸（0~2 段 SOT → 最多 3 塊旁白）────────
        # items 是成片音軌的實際順序：旁白塊與 SOT 段交錯；空旁白塊略過
        # （兩段 SOT 背靠背也成立）。無 SOT 時就是單一旁白塊，流程與過去一致。
        sentences = _split_sentences(script['narration'])
        items = []
        if sots:
            cursor = 0
            for s in sots:
                kk = min(max(int(s['after_sentence']), 1), len(sentences))
                kk = max(kk, cursor)   # 插入點保證遞增
                txt = "".join(sentences[cursor:kk])
                if txt:
                    items.append({'type': 'nar', 'text': txt, 'nsent': kk - cursor})
                items.append({'type': 'sot', 'sot': s})
                cursor = kk
            tail = "".join(sentences[cursor:])
            if tail:
                items.append({'type': 'nar', 'text': tail,
                              'nsent': len(sentences) - cursor})
            if not any(it['type'] == 'nar' for it in items):   # 極端防呆
                items.insert(0, {'type': 'nar', 'text': script['narration'],
                                 'nsent': len(sentences)})
        else:
            items = [{'type': 'nar', 'text': script['narration'],
                      'nsent': len(sentences)}]
        nar_parts = [it['text'] for it in items if it['type'] == 'nar']

        p(3)
        def _tts_all(rate=None):
            """各段旁白並行合成（edge-tts 網路服務，並行純省等待、輸出不變）"""
            def one(pi_text):
                pi, text = pi_text
                pp = TMP / f"_narration_p{pi}.mp3"
                kw = {'voice': voice}
                if rate:
                    kw['rate'] = rate
                b = generate_tts(text, pp, **kw)
                return pp, b, _probe_duration(pp)
            with ThreadPoolExecutor(max_workers=3) as ex:
                res = list(ex.map(one, list(enumerate(nar_parts))))
            return ([r[0] for r in res], [r[1] for r in res], [r[2] for r in res])

        part_paths, part_bounds, part_secs = _tts_all()
        nar_total = sum(part_secs)

        # 長度保險 B：唸完還是太長 → 按比例加快語速重唸（上限 +20%）
        nar_limit = nar_budget_sec + 4
        if nar_total > nar_limit:
            need = 1.08 * nar_total / (nar_budget_sec + 1)
            pct = min(int(round((need - 1) * 100)), 20)
            _job['msg'] = f"{STEPS[3]}（旁白 {nar_total:.0f}s 過長，以 +{pct}% 語速重唸）"
            part_paths, part_bounds, part_secs = _tts_all(rate=f"+{pct}%")
            nar_total = sum(part_secs)

        # 音軌組裝：照 items 順序把旁白塊與 SOT 原音串成單一音軌
        audio_files = []
        ni = 0
        for ii, it in enumerate(items):
            if it['type'] == 'nar':
                audio_files.append(part_paths[ni])
                ni += 1
            else:
                s = it['sot']
                sa = TMP / f"_sot_audio_{ii}.m4a"
                extract_audio_clip(Path(s['path']), s['start'], s['dur'], sa)
                audio_files.append(sa)
        concat_audio(audio_files, tts_path)

        main_sec = round(min(max(nar_total + sot_total + 0.4, 15.0), 90.0), 1)
        _job['main_sec'] = main_sec

        p(4)
        # 走一遍 items 累計成片時間軸：每塊旁白各自逐字對齊再平移；
        # SOT 字幕用校正後引句按比例分配。順便在「純旁白時間軸」上定位
        # 數據卡的插入時間（旁白唸到 stat 數字的那一句）
        card = script.get('card') if isinstance(script.get('card'), dict) else None
        # 插卡定位鍵（依序嘗試）：anchor 原文片段 → value 字面寫法（保留萬/億，
        # 「1萬1400」抽成純數字會跟字幕對不上）→ 純數字備援
        card_keys = []
        if card:
            for k in (card.get('anchor', ''), card.get('value', '')):
                kk = re.sub(r'[,，\s]', '', str(k))
                if len(kk) >= 2:
                    card_keys.append(kk)
            kd = re.sub(r'[^0-9.]', '', str(card.get('value', '')))
            if len(kd) >= 2:
                card_keys.append(kd)
        stat_t = None
        straps = []    # 受訪者名條 [{png, start, end}]（成片時間軸）
        subtitles = []
        align_ok = True
        t_axis = 0.0     # 成片時間軸（含 SOT）
        nar_axis = 0.0   # 純旁白時間軸（配對計畫用的座標系）
        ni = 0
        for it in items:
            if it['type'] == 'nar':
                text = it['text']
                subs_p = _build_subtitles(text)
                aligned = (align_subtitles_to_boundaries(subs_p, part_bounds[ni])
                           if part_bounds[ni] else [])
                if not aligned:
                    align_ok = False
                    aligned = rescale_subtitles(subs_p, float(MAIN_SEC), part_secs[ni])
                if card_keys and stat_t is None:
                    from produce import _ts_to_sec
                    for sb in aligned:
                        txt = re.sub(r'[,，\s]', '', str(sb.get('text', '')))
                        if any(k in txt for k in card_keys):
                            # 字幕 start 是 SRT 時間字串（00:00:05,976），要轉秒數
                            stat_t = round(nar_axis + _ts_to_sec(sb['start']), 2)
                            break
                subtitles += shift_subtitles(aligned, t_axis)
                t_axis += part_secs[ni]
                nar_axis += part_secs[ni]
                ni += 1
            else:
                s = it['sot']
                subtitles += build_sot_subtitles(s['display'], t_axis, s['dur'])
                if s.get('speaker'):   # 受訪者名條圖卡：原音開始 0.3 秒後進、最多掛 5 秒
                    try:
                        from produce import make_strap_png
                        strap_png = TMP / f"_strap_{len(straps)}.png"
                        make_strap_png(s.get('spk_name') or s['speaker'],
                                       s.get('spk_title', ''), strap_png)
                        straps.append({'png': str(strap_png),
                                       'start': round(t_axis + 0.3, 2),
                                       'end': round(t_axis + min(s['dur'], 5.3), 2)})
                    except Exception:
                        pass   # 名條是加分項，畫不出來不擋產製
                t_axis += s['dur']
        subtitles.sort(key=lambda x: x['start'])
        _job['sub_align'] = '逐字對齊' if align_ok else '等比縮放(fallback)'

        # segments（配畫面用）：按塊精算再平移（只涵蓋旁白時間，SOT 是固定畫面不用配）
        # GPT 的 segments 依各旁白塊的句數依序切分
        gpt_seg_parts = []
        rem_segs = script.get('segments', [])
        for it in items:
            if it['type'] != 'nar':
                continue
            part_segs, rem_segs = _split_gpt_segments(rem_segs, it['nsent'])
            gpt_seg_parts.append(part_segs)
        if gpt_seg_parts and rem_segs:   # 句數統計誤差的剩餘 → 併進最後一塊
            gpt_seg_parts[-1] = gpt_seg_parts[-1] + rem_segs
        segments = []
        nar_off = 0.0    # 在「純旁白時間軸」上的偏移（不含 SOT）
        seg_ok = True
        for pi, text in enumerate(nar_parts):
            segs_p = align_segments_to_boundaries(
                text, gpt_seg_parts[pi] if pi < len(gpt_seg_parts) else [],
                part_bounds[pi]) if part_bounds[pi] else []
            if not segs_p:
                seg_ok = False
                segs_p = rescale_segments(text, gpt_seg_parts[pi] if pi < len(gpt_seg_parts) else [],
                                          part_secs[pi])
            for sgp in segs_p:
                segments.append({**sgp, 'start': round(sgp['start'] + nar_off, 1),
                                 'end': round(sgp['end'] + nar_off, 1)})
            nar_off += part_secs[pi]
        _job['seg_align'] = '逐字精算' if seg_ok else '文字比例(fallback)'
        write_ass(subtitles, ass_path, highlights=script.get('highlights') or [])

        # ── 步驟5：旁白配畫面（配的是「純旁白時間軸」，配完在 SOT 點切開插入原音段）──
        # 沒有影片但有照片 → 圖輯式模式：整支用照片輪播（Ken Burns）配旁白
        # 照片圖說優先序：①記者手寫圖說（API A_Photo.Content，主圖才有，最準又免費）
        # → ②GPT 視覺辨識（探測抓的其餘配圖沒圖說，逐張看，有快取）→ ③罐頭描述。
        existing_photos = [ph for ph in (photos or []) if Path(ph).exists()]
        photo_items = []
        for ph in existing_photos:
            cap = ltn_api.get_photo_caption(ph)
            d = cap or describe_photo(Path(ph)) or "新聞報導配圖（事件現場或網傳畫面的截圖）"
            photo_items.append({"path": ph, "desc": d,
                                "desc_src": "記者圖說" if cap else "AI辨識"})
        photo_mode = (not catalogs) and bool(photo_items)
        plan = None
        if (catalogs or photo_items) and segments:
            p(5)
            try:
                sot_note = None
                if sots:
                    spans = "；".join(
                        f"{s['label']} 的 {s['start']}~{round(s['start']+s['dur'],1)} 秒"
                        for s in sots)
                    sot_note = (f"成片會在旁白中插入受訪原音段：{spans}，"
                                "請不要把這些影片區間再拿來當配音底圖")
                plan, match_tokens = match_narration_to_clips(
                    segments, catalogs, round(nar_total, 1),
                    photos=photo_items, notes=sot_note)
                _add_tokens(match_tokens)

                # 資訊卡（數據/時序/警示/圖表）：旁白唸到 anchor 那句時換上動畫卡
                # （1:1 換掉原本配的畫面，總長不變，不影響後面的 SOT 插入座標）
                if card and stat_t is not None and plan:
                    try:
                        import hashlib as _hl
                        import statcard
                        ctype = card.get('type', 'stat')
                        cname = statcard.TYPE_NAMES.get(ctype, '資訊卡')
                        # 同內容的卡有快取：重跑/取消再來不用重畫（省 ~20 秒）
                        card_key = _hl.md5(json.dumps(card, sort_keys=True,
                                                      ensure_ascii=False).encode()
                                           ).hexdigest()[:12]
                        card_path = TMP / f"_statcard_{card_key}.mp4"
                        if not card_path.exists():
                            _job['msg'] = f"{STEPS[5]}（渲染{cname}中）"
                        if card_path.exists() or statcard.render_card(card, card_path):
                            plan_total = sum(float(e.get('dur', 0) or 0) for e in plan)
                            card_dur = round(min(statcard.card_duration(card),
                                                 plan_total - stat_t), 2)
                            if card_dur >= 1.5:
                                gist = (f"{card.get('label', '')}{card.get('value', '')}"
                                        f"{card.get('unit', '')}" if ctype == 'stat'
                                        else card.get('title') or card.get('headline') or '')
                                pA, pB = _split_plan_at(plan, stat_t)
                                _consumed, pRest = _split_plan_at(pB, card_dur)
                                plan = pA + [{'path': str(card_path), 'photo': None,
                                              'start': 0.0, 'dur': card_dur,
                                              'why': f"📊 {cname}：{gist}"}] + pRest
                                _job['stat'] = card
                    except Exception:
                        pass   # 插卡是加分項，失敗不擋產製

                # 黑幕兜底：GPT 配不到畫面的段落，有照片就用照片（Ken Burns）不留黑
                if photo_items and plan:
                    ph_i = 0
                    for e in plan:
                        if e.get('path') or e.get('photo'):
                            continue
                        e['photo'] = photo_items[ph_i % len(photo_items)]['path']
                        e['why'] = (e.get('why') or '') + '（照片補黑幕）'
                        ph_i += 1

                # 資訊卡未插入時明確警告（先前是靜默消失，使用者只能猜）
                if card and not _job.get('stat'):
                    _ck = card.get('anchor') or card.get('value') or ''
                    if stat_t is None:
                        _sk = f"📊 資訊卡未插入：旁白字幕找不到「{_ck}」的落點（anchor 需與旁白字面一致）"
                    else:
                        _sk = "📊 資訊卡未插入：渲染失敗或落點太靠影片結尾（不足 1.5 秒）"
                    _job['warning'] = ((_job.get('warning') + '\n' + _sk)
                                       if _job.get('warning') else _sk)

                # 照 items 順序在各 SOT 插入點把配對時間軸切開，插進原音段
                # （畫面=原影片、聲音已在音軌裡）；配對時間軸是「純旁白時間軸」
                if sots:
                    assembled, rest = [], plan
                    nar_acc = 0.0   # 純旁白時間軸累計
                    cut_acc = 0.0   # rest 已消耗掉的起點
                    ni2 = 0
                    for it in items:
                        if it['type'] == 'nar':
                            nar_acc += part_secs[ni2]
                            ni2 += 1
                        else:
                            s = it['sot']
                            a, rest = _split_plan_at(rest, nar_acc - cut_acc)
                            cut_acc = nar_acc
                            assembled += a + [{'path': s['path'], 'photo': None,
                                               'start': s['start'], 'dur': s['dur'],
                                               'why': f"🎤 SOT 受訪原音：{s['display'][:40]}"}]
                    plan = _merge_tiny_plan_entries(assembled + rest)
                elif _job.get('stat'):   # 沒 SOT 但插了數據卡 → 一樣收掉切出的碎片
                    plan = _merge_tiny_plan_entries(plan)

                _job['plan'] = [
                    {'video': (Path(e['path']).name if e.get('path')
                               else (f"📷 {Path(e['photo']).name}" if e.get('photo') else '（黑幕）')),
                     'start': e.get('start', 0), 'dur': e['dur'],
                     'why': e.get('why', '')}
                    for e in plan
                ]
                # 涵蓋度警告：圖輯式模式（純照片）本來就是照片，不做「照片偏多」提醒；
                # SOT 段是刻意安排的原音（有聲受訪），不算「閉麥講話臉」問題
                if not photo_mode:
                    cover_warn = _coverage_warning(
                        [e for e in plan if not str(e.get('why', '')).startswith('🎤')], catalogs)
                    if cover_warn:
                        _job['warning'] = ((_job.get('warning') + '\n' + cover_warn)
                                           if _job.get('warning') else cover_warn)
            except Exception:
                plan = None  # 配對失敗退回依序銜接

        # ── Checkpoint：分鏡確認（渲染前先讓人看旁白/Hook/配對縮圖/字幕，確認才渲染）──
        # 對治「跑完整支才發現畫面對不上／字幕怪／Hook 怪，又得整支重跑」的痛點。
        # 智慧停站（'smart'）：只在有理由時才停（見 _stop_reasons）；乾淨的自動渲染進成片庫。
        # 'always' 每支都停；'off' 一律不停。
        if checkpoint_mode == 'smart':
            _cp_reasons = _stop_reasons(_job.get('factcheck'), _job.get('translit'),
                                        plan, catalogs)
            _do_stop = bool(_cp_reasons)
        else:
            _cp_reasons = []
            _do_stop = (checkpoint_mode == 'always')
        if _do_stop:
            global _preview
            _preview = _build_preview(script, subtitles, plan or [],
                                      _job.get('factcheck'), _job.get('translit'),
                                      catalogs)
            _preview['stop_reasons'] = _cp_reasons   # 分鏡站最上面直接說明為什麼停
            # 給快速預覽用的渲染上下文（分鏡確認暫停期間有效）
            global _pending_ctx
            _pending_ctx = {'plan': plan or [], 'tts': str(tts_path),
                            'ass': str(ass_path), 'main_sec': main_sec,
                            'hook': _job.get('hook'), 'straps': straps,
                            'title': _job.get('title') or script.get('title', ''),
                            'cat_paths': {str(c['path']) for c in catalogs}}
            _confirm_action[0] = 'go'
            _confirm_edits[0] = None
            _confirm_event.clear()
            _job['awaiting_confirm'] = True
            _job['msg'] = '⏸ 等待分鏡確認（渲染前）'
            got = _confirm_event.wait(timeout=1800)   # 最多等 30 分鐘，逾時視同取消
            _job['awaiting_confirm'] = False
            _preview = {}
            _pending_ctx = {}
            if (not got) or _confirm_action[0] == 'cancel':
                raise _Cancelled()

            # 套用分鏡站的人工換片段＋資訊卡移除（移除＝時長併鄰段，不用重配）
            edits = _confirm_edits[0] or []
            _confirm_edits[0] = None
            removals = (_confirm_meta[0] or {}).get('removals') or []
            applied = 0
            if edits and plan:
                for e in edits:   # 回饋閉環：GPT 原選片段 vs 人換成什麼
                    i = e.get('index')
                    if (isinstance(i, int) and 0 <= i < len(plan)
                            and e.get('path')):
                        seg = plan[i]
                        _append_feedback('clip_swap', job_id, {
                            'title': script.get('title', ''), 'index': i,
                            'before': {'path': str(seg.get('path') or ''),
                                       'photo': str(seg.get('photo') or ''),
                                       'start': seg.get('start', 0),
                                       'why': seg.get('why', '')},
                            'after': {'path': str(e['path']),
                                      'start': e.get('start', 0)}})
                plan, applied = _apply_plan_edits(
                    plan, edits, {str(c['path']) for c in catalogs})
            if removals and plan:
                removed_segs = [
                    {'why': plan[i].get('why', ''), 'dur': plan[i].get('dur')}
                    for i in removals
                    if isinstance(i, int) and 0 <= i < len(plan)
                    and str(plan[i].get('why', '')).startswith('📊')]
                plan, n_removed = _remove_card_entries(plan, removals)
                if n_removed:
                    applied += n_removed
                    _append_feedback('card_removed_at_plan', job_id, {
                        'title': script.get('title', ''),
                        'card': _job.get('stat'),
                        'segments': removed_segs})
                    _job['stat'] = None   # 卡移除了，紀錄同步清掉
            if edits or removals:
                if applied:
                    _job['plan'] = [
                        {'video': (Path(e['path']).name if e.get('path')
                                   else (f"📷 {Path(e['photo']).name}" if e.get('photo') else '（黑幕）')),
                         'start': e.get('start', 0), 'dur': e['dur'],
                         'why': e.get('why', '')}
                        for e in plan
                    ]
                    _job['msg'] = f'已套用 {applied} 處人工換片段，開始渲染'

        # 套用分鏡站改過的開場標題/Hook（空值=沒改）
        meta = _confirm_meta[0] or {}
        _confirm_meta[0] = None
        if meta.get('title'):
            if meta['title'] != (script.get('title') or ''):
                _append_feedback('title_edit', job_id, {
                    'before': script.get('title', ''), 'after': meta['title']})
            script['title'] = meta['title']
            _job['title'] = meta['title']
        if meta.get('hook'):
            if meta['hook'] != (_job.get('hook') or ''):
                _append_feedback('hook_edit', job_id, {
                    'title': script.get('title', ''),
                    'before': _job.get('hook', ''), 'after': meta['hook']})
            _job['hook'] = meta['hook']

        p(6)
        # 冷開場改版（2026-07）：不再做 3 秒片頭卡——研究指出 50~60% 流失發生在
        # 前 3 秒；標題改成疊在主畫面最前 TITLE_SEC 秒的標題條（與 Hook 同組）
        intro = None

        p(7)
        # 用 _job['hook']（不是 script.get('hook')）：長度保險 A 觸發縮寫時，
        # GPT 縮寫後的 JSON 偶爾會漏帶 hook 欄位，_job['hook'] 有防呆保留舊值，
        # 兩處讀不同來源會導致「/logs 顯示有 hook、但實際影片沒燒進去」的落差
        hook = _job.get('hook')
        title = _job.get('title') or script.get('title', '')
        # 只有走 plan 的路徑支援低畫質；無 plan 的純黑幕備援維持全畫質，
        # 片尾要跟主畫面實際畫質一致（concat -c copy 要求解析度相同）
        main_is_draft = bool(low_quality and plan)
        if plan:
            _enrich_subject_pos(plan, catalogs)   # 每段查主體位置，供直式裁切偏移
            main_clip, length_info = make_main_plan(tts_path, ass_path, plan, main_sec,
                                                    hook=hook, straps=straps, title=title,
                                                    draft=main_is_draft)
        else:
            main_clip, length_info = make_main(tts_path, ass_path, videos, main_sec,
                                               hook=hook, title=title)
        if length_info.get("insufficient"):
            short_msg = (
                f"來源畫面只涵蓋 {length_info['covered_sec']} 秒，"
                f"不足 {length_info['target_sec']} 秒，"
                f"其餘 {length_info['shortfall_sec']} 秒為黑幕空景"
            )
            # 不要蓋掉步驟5的涵蓋度提醒，兩則都留
            _job['warning'] = (_job.get('warning') + '\n' + short_msg) if _job.get('warning') else short_msg

        p(8)
        outro = make_outro(draft=main_is_draft)

        p(9)
        safe = re.sub(r'[\\/:*?"<>|]', '-', script['title'])[:20]
        name = (fname.strip() if fname else safe) or safe
        if not name.endswith('.mp4'):
            name += '.mp4'
        out_dir = OUTPUT / datetime.datetime.now().strftime('%y%m%d')   # 依產製日期分資料夾
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / name
        concat([c for c in (intro, main_clip, outro) if c], out_path)
        output_name = name
        step_durations[STEPS[9]] = round(time.time() - _last_step_t[0], 1)

        for f in [tts_path, ass_path]:
            try:
                f.unlink()
            except Exception:
                pass

        _job.update({'step': 9, 'msg': '完成', 'done': True, 'filename': name,
                     'elapsed_sec': round(time.time() - t0, 1),
                     'step_durations': dict(step_durations)})

    except _Cancelled:
        # 使用者在分鏡確認按了取消：正常中止、不算錯誤，清掉暫存
        for f in [tts_path, ass_path]:
            try:
                f.unlink()
            except Exception:
                pass
        _job.update({'done': True, 'cancelled': True, 'awaiting_confirm': False,
                     'awaiting_script': False,
                     'msg': '已取消（確認未通過，未渲染）',
                     'elapsed_sec': round(time.time() - t0, 1),
                     'step_durations': dict(step_durations)})

    except Exception as e:
        err_msg = str(e)
        if _job['step'] > 0:
            step_durations[STEPS[_job['step']]] = round(time.time() - _last_step_t[0], 1)
        _job.update({'done': True, 'error': err_msg,
                     'elapsed_sec': round(time.time() - t0, 1),
                     'step_durations': dict(step_durations)})

    finally:
        with _lock:
            _busy = False
        _append_log({
            "id":                 job_id,
            "type":               "produce",
            "timestamp":          datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_sec":       round(time.time() - t0, 1),
            "model":              model,
            "tts_voice":          voice,
            "article_chars":      len(article),
            "video_path":         "; ".join(v["path"] for v in videos) if videos else "",
            "video_count":        len(videos),
            "video_covered_sec":  length_info.get("covered_sec", 0),
            "video_shortfall_sec": length_info.get("shortfall_sec", 0),
            "output_file":        output_name or "",
            "prompt_tokens":      token_usage.get("prompt_tokens", 0),
            "completion_tokens":  token_usage.get("completion_tokens", 0),
            "total_tokens":       token_usage.get("total_tokens", 0),
            "frames_sent":        token_usage.get("frames_sent", 0),
            "matched":            bool(_job.get('plan')),
            "sub_align":          _job.get('sub_align', ''),
            "seg_align":          _job.get('seg_align', ''),
            "sot":                _job.get('sot'),
            "stat":               _job.get('stat'),
            "photos":             photos or [],
            "factcheck":          _job.get('factcheck'),
            "translit":           _job.get('translit'),
            "main_sec":           _job.get('main_sec', 0),
            "low_quality":        bool(low_quality),
            "step_durations":     step_durations,
            "cancelled":          bool(_job.get('cancelled')),
            "warning":            _job.get('warning') or "",
            "error":              err_msg,
            # 產製歷史回溯用：旁白全文、標題/hook/hashtag、配對明細、原始新聞稿
            "article":            article,
            "title":              script.get('title', ''),
            "hook":               script.get('hook', ''),
            "hashtags":           script.get('hashtags', []),
            "narration":          script.get('narration', ''),
            "plan":               _job.get('plan', []),
        })


def run_analysis(video_path: str, use_vision: bool):
    global _abusy, _analysis

    t0 = time.time()
    job_id = str(uuid.uuid4())[:8]
    model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    err_msg: str | None = None

    def _cb(layer: int, msg: str):
        _analysis.update({'layer': layer, 'msg': msg})

    try:
        result = select_clip(Path(video_path), use_vision=use_vision, progress_cb=_cb)
        _analysis.update({'done': True, 'result': result, 'msg': '分析完成'})

        tokens = result.get("tokens", {})
        _append_log({
            "id":                job_id,
            "type":              "analyze",
            "timestamp":         datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_sec":      round(time.time() - t0, 1),
            "model":             model if use_vision else "(heuristic only)",
            "tts_voice":         "",
            "article_chars":     0,
            "video_path":        video_path,
            "video_start_sec":   result.get("start_sec", 0),
            "output_file":       "",
            "prompt_tokens":     tokens.get("prompt_tokens", 0),
            "completion_tokens": tokens.get("completion_tokens", 0),
            "total_tokens":      tokens.get("total_tokens", 0),
            "frames_sent":       tokens.get("frames_sent", 0),
            "category":          result.get("category", ""),
            "method":            result.get("method", ""),
            "error":             None,
        })

    except Exception as e:
        err_msg = str(e)
        _analysis.update({'done': True, 'error': err_msg})
        _append_log({
            "id":               job_id,
            "type":             "analyze",
            "timestamp":        datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_sec":     round(time.time() - t0, 1),
            "model":            model,
            "tts_voice":        "", "article_chars": 0,
            "video_path":       video_path, "video_start_sec": 0,
            "output_file":      "",
            "prompt_tokens":    0, "completion_tokens": 0,
            "total_tokens":     0, "frames_sent": 0,
            "category":         "", "method":      "",
            "error":            err_msg,
        })
    finally:
        with _alock:
            _abusy = False


# ── 使用記錄頁面 HTML ────────────────────────────────────────────────────────
GALLERY_HTML = """<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>成片庫 — VideoAI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:17px}
body{font-family:-apple-system,"Microsoft JhengHei",sans-serif;background:#f3f4f6;color:#111;padding:24px}
.nav{display:flex;gap:6px;align-items:center;margin-bottom:20px}
.nav h1{font-size:1.2rem;font-weight:700;margin-right:auto}
.nav a{font-size:.83rem;color:#6b7280;text-decoration:none;padding:7px 14px;border:1px solid #d1d5db;border-radius:7px;background:#fff}
.nav a.active{background:#2563eb;color:#fff;border-color:#2563eb}
.nav a:hover:not(.active){background:#f9fafb}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.bar input{padding:8px 12px;border:1px solid #d1d5db;border-radius:7px;font-size:.85rem;width:240px}
.count{color:#6b7280;font-size:.82rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px;max-width:1240px;margin-left:auto;margin-right:auto}
.nav,.bar{max-width:1240px;margin-left:auto;margin-right:auto}
.card{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.09);display:flex;flex-direction:column}
.card video{width:100%;aspect-ratio:9/16;background:#000;object-fit:contain;display:block}
.card .body{padding:12px 14px;display:flex;flex-direction:column;gap:6px;flex:1}
.card .t{font-weight:600;font-size:.9rem;line-height:1.4}
.card .meta{color:#9ca3af;font-size:.74rem}
.card .tags{color:#2563eb;font-size:.74rem;line-height:1.5}
.card .fc{color:#b91c1c;font-size:.74rem;font-weight:600}
.card .acts{display:flex;gap:8px;margin-top:auto;padding-top:8px}
.card .acts button,.card .acts a{flex:1;text-align:center;padding:6px 0;border-radius:6px;font-size:.78rem;font-weight:600;cursor:pointer;text-decoration:none;border:none}
.b-dl{background:#16a34a;color:#fff}
.b-dl:hover{background:#15803d}
.b-re{background:#374151;color:#fff}
.b-re:hover{background:#111}
.b-de{background:#fee2e2;color:#991b1b}
.b-de:hover{background:#fecaca}
.b-detail{background:#eff6ff;color:#1d4ed8}
.empty{padding:60px 20px;text-align:center;color:#9ca3af}
.detail-wrap{display:none;padding:12px 14px;border-top:1px solid #f0f0f0;font-size:.8rem;line-height:1.7}
.detail-wrap.show{display:block}
.detail-wrap b{color:#374151}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#111;color:#fff;padding:10px 20px;border-radius:8px;font-size:.85rem;opacity:0;transition:opacity .3s;pointer-events:none;z-index:100}
.toast.show{opacity:1}
</style>
</head>
<body>
<div class="nav">
  <h1>🎬 成片庫</h1>
  <a href="/">產製</a>
  <a href="/gallery" class="active">成片庫</a>
  <a href="/logs">使用記錄</a>
</div>
<div class="bar">
  <input type="text" id="kw" placeholder="搜尋標題／關鍵字…" oninput="render()">
  <span class="count" id="count"></span>
</div>
<div class="grid" id="grid"></div>
<div class="toast" id="toast"></div>
<script>
let _all = [];
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200);}
function askConfirm(msg,onYes){
  const ov=document.createElement('div');
  ov.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:300;display:flex;align-items:center;justify-content:center';
  ov.innerHTML=`<div style="background:#fff;border-radius:12px;padding:20px 22px;max-width:360px;width:90%;box-shadow:0 10px 40px rgba(0,0,0,.3)">
    <div style="font-size:.92rem;color:#111;line-height:1.65">${msg}</div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="ok" style="flex:1;padding:9px;border:0;border-radius:8px;background:#7c3aed;color:#fff;font-weight:700;cursor:pointer">確定</button>
      <button class="no" style="flex:1;padding:9px;border:1px solid #d1d5db;border-radius:8px;background:#fff;color:#374151;cursor:pointer">取消</button>
    </div></div>`;
  ov.querySelector('.ok').onclick=()=>{ov.remove();onYes();};
  ov.querySelector('.no').onclick=()=>ov.remove();
  ov.onclick=e=>{if(e.target===ov)ov.remove();};
  document.body.appendChild(ov);
}

async function load(){
  _all = await (await fetch('/api/gallery')).json();
  render();
}
function render(){
  const kw = document.getElementById('kw').value.toLowerCase();
  const rows = _all.filter(r => !kw ||
    (r.title+' '+(r.hashtags||[]).join(' ')+' '+(r.article||'')).toLowerCase().includes(kw));
  document.getElementById('count').textContent = `${rows.length} 支成片`;
  const grid = document.getElementById('grid');
  if(!rows.length){grid.innerHTML='<div class="empty">目前沒有成片，去「產製」做一支吧</div>';return;}
  grid.innerHTML = rows.map((r,i) => {
    const vurl = '/api/video/'+encodeURIComponent(r.filename);
    const durl = '/api/download/'+encodeURIComponent(r.filename);
    const ts = (r.timestamp||'').replace('T',' ').slice(0,16);
    const tags = (r.hashtags||[]).join(' ');
    const fc = (r.factcheck&&r.factcheck.length)?`<div class="fc">🔍 查核有 ${r.factcheck.length} 處出入（未修）</div>`:'';
    const sot = r.sot?`<div class="meta">🎤 含受訪原音${(Array.isArray(r.sot)&&r.sot.length>1)?' ×'+r.sot.length:''}</div>`:'';
    return `<div class="card">
      <video src="${vurl}" data-poster="/api/thumb/${encodeURIComponent(r.filename)}" preload="none" controls playsinline></video>
      <div class="body">
        <div class="t">${esc(r.title)}</div>
        <div class="meta">${ts}・${r.main_sec||0}秒・$${(r.cost||0).toFixed(3)}</div>
        ${sot}${fc}
        ${tags?`<div class="tags">${esc(tags)}</div>`:''}
        <div class="acts">
          <a class="b-dl" href="${durl}">⬇下載</a>
          <button class="b-re" onclick="rerun('${esc(r.filename)}')">🔄重跑</button>
          <button class="b-detail" onclick="toggleDetail(${i})">詳情</button>
          <button class="b-de" onclick="del('${esc(r.filename)}')">刪</button>
        </div>
      </div>
      <div class="detail-wrap" id="d${i}">
        <div><b>Hook：</b>${esc(r.hook)}</div>
        <div style="margin-top:6px"><b>旁白：</b>${esc(r.narration)}</div>
        ${(r.plan&&r.plan.length)?`<div style="margin-top:6px"><b>配對：</b>${r.plan.map(p=>esc(p.video)+'('+(p.dur||0).toFixed(0)+'s)').join('、')}</div>`:''}
        ${r.video_path?`<div style="margin-top:6px"><b>素材：</b>${esc(r.video_path.split(';').map(s=>s.split(/[\\\\/]/).pop()).join('、'))}</div>`:''}
      </div>
    </div>`;
  }).join('');
  lazyPosters();
}
// 懶載入封面縮圖：只有捲進視窗的卡片才去抽首幀（避免一次 30 支同時抽幀卡住）
let _po = null;
function lazyPosters(){
  if(_po) _po.disconnect();
  _po = new IntersectionObserver((ents)=>{
    ents.forEach(e=>{
      if(e.isIntersecting){
        const v=e.target;
        if(v.dataset.poster){v.poster=v.dataset.poster;delete v.dataset.poster;}
        _po.unobserve(v);
      }
    });
  },{rootMargin:'200px'});
  document.querySelectorAll('.card video[data-poster]').forEach(v=>_po.observe(v));
}
function toggleDetail(i){document.getElementById('d'+i).classList.toggle('show');}
function rerun(fn){
  askConfirm('用這支的原始新聞稿＋影片重新產製一支？<br><span style="color:#6b7280;font-size:.82rem">（分析有快取，會很快）</span>', async()=>{
    const d = await (await fetch('/api/rerun',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:fn})})).json();
    if(d.error){toast('重跑失敗：'+d.error);return;}
    let msg='已開始重跑，到「產製」頁看進度';
    if(d.videos_found<d.videos_expected)msg+=`（${d.videos_expected}支素材只找到${d.videos_found}支，其餘可能已刪）`;
    toast(msg);
    setTimeout(()=>location.href='/',1500);
  });
}
function del(fn){
  askConfirm('確定刪除這支成片？<br><span style="color:#b91c1c;font-size:.82rem">此動作無法復原</span>', async()=>{
    const d = await (await fetch('/api/gallery/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:fn})})).json();
    if(d.error){toast('刪除失敗：'+d.error);return;}
    toast('已刪除');load();
  });
}
load();
</script>
</body>
</html>"""


LOGS_HTML = """<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>使用記錄 — VideoAI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:17px}
body{font-family:-apple-system,"Microsoft JhengHei",sans-serif;background:#f3f4f6;color:#111;padding:24px}
h1{font-size:1.15rem;font-weight:700;margin-bottom:18px}
.back{font-size:.82rem;color:#6b7280;text-decoration:none;margin-right:14px}
.back:hover{color:#111}
.stat-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.topnav,.stat-row,.price-row,.filter-row,table,.tbl-wrap{max-width:1240px;margin-left:auto;margin-right:auto}
.stat{background:#fff;border-radius:8px;padding:14px 18px;flex:1;min-width:130px;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.stat .val{font-size:1.4rem;font-weight:700;color:#2563eb}
.stat .lbl{font-size:.75rem;color:#888;margin-top:2px}
.price-row{display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:.83rem;color:#555}
.price-row input{width:90px;padding:5px 8px;border:1px solid #d1d5db;border-radius:5px;font-size:.83rem}
.tbl-wrap{overflow-x:auto;background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.07)}
table{width:100%;min-width:1440px;border-collapse:collapse;font-size:.82rem;table-layout:fixed}
th{background:#f9fafb;padding:10px 12px;text-align:left;font-weight:600;color:#555;border-bottom:2px solid #e5e7eb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td{padding:9px 12px;border-bottom:1px solid #f0f0f0;vertical-align:middle;color:#374151;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.num,th.num{text-align:right}
td.wrap{white-space:normal}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:600}
.badge-produce{background:#dbeafe;color:#1d4ed8}
.badge-analyze{background:#f3e8ff;color:#6d28d9}
.badge-err{background:#fee2e2;color:#b91c1c}
.badge-ok{background:#dcfce7;color:#166534}
.mono{font-family:monospace;font-size:.8rem}
.na{color:#ccc}
.filter-row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.filter-row select,.filter-row input{padding:6px 10px;border:1px solid #d1d5db;border-radius:6px;font-size:.82rem}
.btn-export{padding:6px 14px;background:#374151;color:#fff;border:none;border-radius:6px;font-size:.82rem;cursor:pointer}
.btn-export:hover{background:#111}
.topnav{display:flex;gap:6px;align-items:center;margin-bottom:20px}
.topnav h1{font-size:1.2rem;font-weight:700;margin:0 auto 0 0}
.topnav a{font-size:.83rem;color:#6b7280;text-decoration:none;padding:7px 14px;border:1px solid #d1d5db;border-radius:7px;background:#fff}
.topnav a.active{background:#2563eb;color:#fff;border-color:#2563eb}
.topnav a:hover:not(.active){background:#f9fafb}
.btn-rerun{padding:4px 10px;background:#374151;color:#fff;border:none;border-radius:5px;font-size:.76rem;cursor:pointer}
.btn-rerun:hover{background:#111}
</style>
</head>
<body>
<div class="topnav">
  <h1>📋 使用記錄</h1>
  <a href="/">產製</a>
  <a href="/gallery">成片庫</a>
  <a href="/logs" class="active">使用記錄</a>
</div>

<!-- 統計卡片 -->
<div class="stat-row" id="stats"></div>

<!-- 價格設定 -->
<div class="price-row">
  <span>估算費用單價（USD / 1M tokens）</span>
  <span>Prompt</span>
  <input type="number" id="p-price" value="1.75" step="0.05" min="0">
  <span>Completion</span>
  <input type="number" id="c-price" value="14" step="0.5" min="0">
  <span style="color:#aaa;font-size:.77rem">（Azure 實際費用請查帳單，此為目前部署模型 gpt-5.2 的參考定價，2026-07 查證）</span>
</div>

<!-- 篩選 -->
<div class="filter-row">
  <select id="f-type" onchange="render()">
    <option value="">全部類型</option>
    <option value="produce">產製</option>
    <option value="analyze">智慧分析</option>
  </select>
  <select id="f-status" onchange="render()">
    <option value="">全部狀態</option>
    <option value="ok">成功</option>
    <option value="err">失敗</option>
  </select>
  <input type="text" id="f-keyword" placeholder="關鍵字篩選…" oninput="render()" style="width:180px">
  <button class="btn-export" onclick="exportCSV()">⬇ 匯出 CSV</button>
</div>

<div class="tbl-wrap">
<table>
  <colgroup>
    <col style="width:145px"><col style="width:66px"><col style="width:130px">
    <col style="width:60px"><col style="width:92px"><col style="width:100px">
    <col style="width:62px"><col style="width:92px"><col style="width:72px">
    <col style="width:110px"><col style="width:84px"><col style="width:230px">
    <col style="width:66px"><col style="width:130px">
  </colgroup>
  <thead>
    <tr>
      <th>時間</th>
      <th>類型</th>
      <th>模型</th>
      <th class="num">時長(s)</th>
      <th class="num">Prompt<br>tokens</th>
      <th class="num">Completion<br>tokens</th>
      <th class="num">影格數</th>
      <th class="num">估算費用<br>(USD)</th>
      <th class="num">稿件字數</th>
      <th class="num">影片長度</th>
      <th>分類</th>
      <th>輸出檔</th>
      <th>狀態</th>
      <th>詳情</th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>
</div>

<script>
let _allLogs = [];

async function loadLogs() {
  const d = await (await fetch('/api/logs?limit=500')).json();
  _allLogs = d;
  renderStats(d);
  render();
  // 若有未完成的工作，每 3 秒更新一次
  if (d.length && !d[0].error && d[0].output_file === '' && d[0].type === 'produce') {
    setTimeout(loadLogs, 3000);
  }
}

function renderStats(logs) {
  const total = logs.length;
  const ok    = logs.filter(r => !r.error).length;
  const sumTok = logs.reduce((s,r) => s + (r.total_tokens||0), 0);
  const sumDur = logs.reduce((s,r) => s + (r.duration_sec||0), 0);
  const pp = parseFloat(document.getElementById('p-price').value) || 1.75;
  const cp = parseFloat(document.getElementById('c-price').value) || 14;
  const cost = logs.reduce((s,r) => s
    + (r.prompt_tokens||0)/1e6*pp
    + (r.completion_tokens||0)/1e6*cp, 0);

  document.getElementById('stats').innerHTML = [
    {val: total,              lbl: '總工作數'},
    {val: ok,                 lbl: '成功'},
    {val: fmtSec(sumDur),     lbl: '累計執行時長'},
    {val: fmtNum(sumTok),     lbl: '累計 tokens'},
    {val: '$' + cost.toFixed(4), lbl: '估算總費用 (USD)'},
  ].map(s => `<div class="stat"><div class="val">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
}

function render() {
  const fType = document.getElementById('f-type').value;
  const fStatus = document.getElementById('f-status').value;
  const kw = document.getElementById('f-keyword').value.toLowerCase();
  const pp = parseFloat(document.getElementById('p-price').value) || 1.75;
  const cp = parseFloat(document.getElementById('c-price').value) || 14;

  let rows = _allLogs.filter(r => {
    if (fType && r.type !== fType) return false;
    if (fStatus === 'ok' && r.error) return false;
    if (fStatus === 'err' && !r.error) return false;
    if (kw && !JSON.stringify(r).toLowerCase().includes(kw)) return false;
    return true;
  });

  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map((r, i) => {
    const cost = ((r.prompt_tokens||0)/1e6*pp + (r.completion_tokens||0)/1e6*cp);
    const ts = r.timestamp.replace('T',' ');
    const typeBadge = r.type === 'produce'
      ? '<span class="badge badge-produce">產製</span>'
      : '<span class="badge badge-analyze">分析</span>';
    const statusBadge = r.error
      ? `<span class="badge badge-err" title="${r.error}">失敗</span>`
      : '<span class="badge badge-ok">成功</span>';
    const na = '<span class="na">—</span>';
    const hasDetail = r.type === 'produce' && (r.narration || r.output_file);
    // 失敗的產製給「重跑」按鈕（原始輸入還在，分析有快取，重跑很快）
    const rerunBtn = (r.type === 'produce' && r.error && r.article)
      ? `<button class="btn-rerun" onclick="rerunJob('${r.id}')">🔄 重跑</button>`
      : '';
    const detailBtn = (hasDetail
      ? `<button class="btn-export" style="padding:3px 10px;font-size:.76rem" onclick="toggleDetail(${i})">🔍 詳情</button>`
      : (rerunBtn ? '' : na)) + rerunBtn;
    return `<tr>
      <td class="mono" title="${ts}">${ts}</td>
      <td>${typeBadge}</td>
      <td class="mono" style="font-size:.75rem" title="${r.model||''}">${r.model||na}</td>
      <td class="num">${r.duration_sec||0}</td>
      <td class="num">${fmtNum(r.prompt_tokens)}</td>
      <td class="num">${fmtNum(r.completion_tokens)}</td>
      <td class="num">${r.frames_sent||na}</td>
      <td class="mono num">${cost > 0 ? '$'+cost.toFixed(5) : na}</td>
      <td class="num">${r.article_chars||na}</td>
      <td class="num" title="${(r.video_count||0)+'支 / '+(r.video_covered_sec||0)+'s'}">${videoLenCell(r)}</td>
      <td title="${r.category||''}">${r.category||na}</td>
      <td title="${r.output_file||''}">${r.output_file||na}</td>
      <td>${statusBadge}</td>
      <td>${detailBtn}</td>
    </tr>` + (hasDetail ? `<tr id="det-${i}" style="display:none"><td colspan="14" class="wrap" style="background:#fafafa">${detailHtml(r)}</td></tr>` : '');
  }).join('');

  renderStats(rows);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function toggleDetail(i) {
  const el = document.getElementById('det-' + i);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

function toast(m){
  let t = document.getElementById('toast2');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast2';
    t.style.cssText = 'position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#111;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;z-index:400;opacity:0;transition:opacity .3s;pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent = m; t.style.opacity = '1';
  setTimeout(() => t.style.opacity = '0', 2200);
}
function askConfirm(msg, onYes){
  const ov = document.createElement('div');
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:300;display:flex;align-items:center;justify-content:center';
  ov.innerHTML = `<div style="background:#fff;border-radius:12px;padding:20px 22px;max-width:360px;width:90%;box-shadow:0 10px 40px rgba(0,0,0,.3)">
    <div style="font-size:.92rem;color:#111;line-height:1.65">${msg}</div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="ok" style="flex:1;padding:9px;border:0;border-radius:8px;background:#7c3aed;color:#fff;font-weight:700;cursor:pointer">確定</button>
      <button class="no" style="flex:1;padding:9px;border:1px solid #d1d5db;border-radius:8px;background:#fff;color:#374151;cursor:pointer">取消</button>
    </div></div>`;
  ov.querySelector('.ok').onclick = () => { ov.remove(); onYes(); };
  ov.querySelector('.no').onclick = () => ov.remove();
  ov.onclick = e => { if (e.target === ov) ov.remove(); };
  document.body.appendChild(ov);
}
function rerunJob(id) {
  askConfirm('用這筆的原始新聞稿＋影片重新產製？<br><span style="color:#6b7280;font-size:.82rem">（分析有快取，會很快）</span>', async () => {
    try {
      const d = await (await fetch('/api/rerun', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id})})).json();
      if (d.error) { toast('重跑失敗：' + d.error); return; }
      toast('已開始重跑，前往「產製」頁看進度');
      setTimeout(() => location.href = '/', 1500);
    } catch(e) { toast('重跑失敗：' + e.message); }
  });
}

function detailHtml(r) {
  const blocks = [];
  if (r.title || r.hook) {
    blocks.push(`<div style="margin-bottom:8px">
      <b>標題：</b>${escapeHtml(r.title)} &nbsp; <b>Hook：</b>${escapeHtml(r.hook)}
      ${r.hashtags && r.hashtags.length ? '<br><b>Hashtags：</b>' + escapeHtml(r.hashtags.join(' ')) : ''}
    </div>`);
  }
  if (r.narration) {
    blocks.push(`<div style="margin-bottom:8px"><b>旁白全文：</b><div style="white-space:pre-wrap;line-height:1.6;margin-top:4px">${escapeHtml(r.narration)}</div></div>`);
  }
  if (r.plan && r.plan.length) {
    let at = 0;
    const rows = r.plan.map(e => {
      const from = at.toFixed(0); at += (e.dur||0); const to = at.toFixed(0);
      const src = e.video === '（黑幕）' ? '<span style="color:#999">（黑幕）</span>' : `${escapeHtml(e.video)} ${e.start}s起`;
      return `${from}-${to}s ← ${src}${e.why ? ' — <span style="color:#999">'+escapeHtml(e.why)+'</span>' : ''}`;
    }).join('<br>');
    blocks.push(`<div style="margin-bottom:8px"><b>旁白配對畫面明細：</b><div style="font-family:monospace;font-size:.78rem;margin-top:4px">${rows}</div></div>`);
  }
  if (r.video_path) {
    blocks.push(`<div style="margin-bottom:8px"><b>使用的影片：</b>${escapeHtml(r.video_path)}</div>`);
  }
  if (r.article) {
    blocks.push(`<div style="margin-bottom:8px"><b>原始新聞稿：</b><div style="white-space:pre-wrap;line-height:1.6;margin-top:4px;color:#555">${escapeHtml(r.article)}</div></div>`);
  }
  if (r.output_file) {
    const dlUrl = '/api/download/' + encodeURIComponent(r.output_file);
    blocks.push(`<div><video controls style="width:280px;border-radius:8px;background:#000" src="${dlUrl}"></video> <a href="${dlUrl}" download style="margin-left:8px">⬇ 下載</a></div>`);
  }
  return blocks.join('') || '<span class="na">沒有可顯示的詳情</span>';
}

function videoLenCell(r) {
  const na = '<span class="na">—</span>';
  if (r.type === 'analyze') {
    return (r.video_start_sec != null && r.video_start_sec !== '') ? r.video_start_sec + 's' : na;
  }
  if (r.video_covered_sec == null) return na;
  const shortfall = r.video_shortfall_sec || 0;
  const countTxt = r.video_count ? r.video_count + '支/' : '';
  return shortfall > 0.05
    ? `${countTxt}${r.video_covered_sec}s <span style="color:#b45309">(缺${shortfall}s)</span>`
    : `${countTxt}${r.video_covered_sec}s`;
}

function fmtNum(n) { return n ? n.toLocaleString() : '<span class="na">0</span>'; }
function fmtSec(s) {
  s = Math.round(s);
  if (s < 60) return s + 's';
  const m = Math.floor(s/60), sec = s%60;
  return m + 'm ' + sec + 's';
}

function exportCSV() {
  const pp = parseFloat(document.getElementById('p-price').value) || 1.75;
  const cp = parseFloat(document.getElementById('c-price').value) || 14;
  const headers = ['時間','類型','模型','時長(s)','Prompt tokens','Completion tokens',
                   '影格數','估算費用USD','稿件字數','影片數','涵蓋秒','缺口秒','分類','輸出檔','狀態','錯誤'];
  const rows = _allLogs.map(r => [
    r.timestamp, r.type, r.model, r.duration_sec,
    r.prompt_tokens, r.completion_tokens, r.frames_sent,
    ((r.prompt_tokens||0)/1e6*pp+(r.completion_tokens||0)/1e6*cp).toFixed(5),
    r.article_chars, r.video_count || '', r.video_covered_sec ?? r.video_start_sec ?? '',
    r.video_shortfall_sec || '', r.category, r.output_file,
    r.error ? '失敗' : '成功', r.error||''
  ]);
  const csv = [headers, ...rows].map(r => r.map(v => '"'+(v||'').toString().replace(/"/g,'""')+'"').join(',')).join('\\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,\\uFEFF' + encodeURIComponent(csv);
  a.download = 'videoai_logs.csv';
  a.click();
}

document.getElementById('p-price').addEventListener('input', render);
document.getElementById('c-price').addEventListener('input', render);

loadLogs();
</script>
</body>
</html>"""

# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = """<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>自由短影音產製（內部測試）</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:17px}
body{font-family:-apple-system,"Microsoft JhengHei",sans-serif;background:#f3f4f6;color:#111;padding:28px 24px}
h1{font-size:1.25rem;font-weight:700;margin-bottom:22px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;max-width:1000px;margin-left:auto;margin-right:auto}
.topnav,.backlog-card{max-width:1000px;margin-left:auto;margin-right:auto}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
.card{background:#fff;border-radius:10px;padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{font-size:.9rem;font-weight:600;color:#666;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #f0f0f0}
label{display:block;font-size:.82rem;font-weight:500;color:#555;margin:14px 0 5px}
label:first-of-type{margin-top:0}
textarea,input[type=text],input[type=number]{
  width:100%;padding:9px 11px;border:1px solid #d1d5db;border-radius:6px;
  font-size:1rem;font-family:inherit;background:#fafafa;color:#111;
  transition:border .15s
}
textarea:focus,input:focus{outline:none;border-color:#2563eb;background:#fff}
textarea{resize:vertical;min-height:210px}
.hint{font-size:.8rem;color:#999;margin-top:3px}
.btn{
  display:block;width:100%;margin-top:18px;padding:12px;
  background:#2563eb;color:#fff;border:none;border-radius:7px;
  font-size:.95rem;font-weight:600;cursor:pointer;transition:background .15s
}
.btn:hover{background:#1d4ed8}
.btn:disabled{background:#93c5fd;cursor:not-allowed}

/* 步驟列表 */
.steps{list-style:none}
.steps li{
  padding:9px 0;border-bottom:1px solid #f5f5f5;
  font-size:.87rem;color:#ccc;transition:color .2s
}
.steps li:last-child{border-bottom:none}
.steps li.active{color:#2563eb;font-weight:600}
.steps li.done{color:#16a34a}
.step-row{display:flex;align-items:center;gap:10px}
.icon{width:20px;text-align:center;flex-shrink:0;font-size:.95rem}
.step-time{margin-left:auto;font-size:.76rem;color:#999;font-family:monospace;transition:color .2s}
/* 執行中的秒數用紅色，跑完恢復灰色 */
.step-time.running{color:#dc2626;font-weight:600}
/* 每個步驟自己的細進度條 */
.step-mini{
  height:4px;border-radius:3px;background:#eef0f3;overflow:hidden;
  margin-top:6px;margin-left:30px;display:none
}
.steps li.active .step-mini,.steps li.done .step-mini{display:block}
.step-mini-bar{
  height:100%;width:0%;border-radius:3px;
  background:linear-gradient(90deg,#2563eb,#7c3aed);transition:width .3s ease
}
.steps li.done .step-mini-bar{width:100%!important;background:#16a34a}

/* 進度條 */
.progress-wrap{background:#eef0f3;border-radius:6px;height:9px;overflow:hidden;margin-bottom:8px}
.progress-bar{height:100%;width:0%;background:linear-gradient(90deg,#2563eb,#7c3aed);transition:width .35s ease}
.progress-info{display:flex;justify-content:space-between;font-size:.78rem;color:#666;margin-bottom:16px}
.progress-info b{color:#2563eb}

/* 結果 & 錯誤 */
.result{margin-top:18px;padding:14px 16px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;display:none}
.result p{font-size:.87rem;color:#166534;margin-bottom:10px;font-weight:500}
.dl{
  display:inline-block;padding:7px 18px;background:#16a34a;
  color:#fff;border-radius:5px;text-decoration:none;
  font-size:.87rem;font-weight:600
}
.dl:hover{background:#15803d}
.err{
  margin-top:18px;padding:13px 15px;background:#fef2f2;
  border:1px solid #fca5a5;border-radius:8px;
  font-size:.84rem;color:#b91c1c;display:none;
  word-break:break-all
}
.warn{
  margin-top:18px;padding:13px 15px;background:#fffbeb;
  border:1px solid #fde68a;border-radius:8px;
  font-size:.84rem;color:#92400e;display:none;
  word-break:break-all
}

/* 多影片清單（含每支的智慧分析結果） */
.video-list{margin:10px 0 14px}
.video-item{
  padding:8px 10px;
  background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;margin-bottom:6px
}
.video-item .vtop{display:flex;align-items:center;gap:8px}
.video-item .vidx{
  flex-shrink:0;width:20px;height:20px;border-radius:50%;background:#374151;
  color:#fff;font-size:.72rem;font-weight:700;display:flex;
  align-items:center;justify-content:center
}
.video-item .vname{
  flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-family:monospace;font-size:.78rem;color:#374151
}
.video-item .btn-remove{
  background:none;border:none;color:#ef4444;cursor:pointer;
  font-size:1rem;padding:2px 6px;line-height:1
}
.video-item .btn-remove:hover{color:#b91c1c}
.video-item .btn-preview{
  background:none;border:1px solid #d1d5db;border-radius:6px;color:#374151;
  cursor:pointer;font-size:.72rem;padding:2px 8px;line-height:1.4;flex-shrink:0
}
.video-item .btn-preview:hover{border-color:#374151;background:#f3f4f6}
.video-item .vanalysis{margin-top:6px;padding-left:28px;font-size:.78rem}
.ana-loading{color:#7c3aed}
.ana-err{color:#b91c1c}
.ana-desc-inline{color:#555}
.ana-dur{color:#374151;font-weight:600;font-family:monospace;font-size:.76rem}

.btn-browse{
  padding:9px 14px;background:#374151;color:#fff;border:none;
  border-radius:6px;font-size:.83rem;font-weight:600;
  cursor:pointer;white-space:nowrap;transition:background .15s;flex-shrink:0
}
.btn-browse:hover{background:#1f2937}

/* 後臺影音清單 */
.backlog-card{
  background:#fff;border-radius:12px;padding:20px 24px;
  box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:20px
}
.backlog-filters{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:14px}
.backlog-filters label{font-size:.76rem;color:#6b7280;display:block;margin-bottom:4px}
.backlog-filters input, .backlog-filters select{
  padding:7px 9px;border:1px solid #d1d5db;border-radius:6px;font-size:.85rem
}
.backlog-list{max-height:340px;overflow-y:auto;border:1px solid #e5e7eb;border-radius:8px}
.backlog-row{
  display:grid;grid-template-columns:96px 118px 1fr 210px 78px;
  align-items:center;gap:10px;padding:9px 12px;
  border-bottom:1px solid #f3f4f6;font-size:.82rem
}
.backlog-row:last-child{border-bottom:none}
.backlog-row .bl-title{color:#1f2937;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.backlog-row .bl-meta{color:#9ca3af;font-size:.74rem;white-space:nowrap;text-align:right;overflow:hidden;text-overflow:ellipsis}
/* 表頭：與資料列共用同一組欄寬 → 欄位對齊 */
.backlog-head{
  display:grid;grid-template-columns:96px 118px 1fr 210px 78px;
  align-items:center;gap:10px;padding:8px 12px;margin-top:8px;
  background:#f9fafb;border:1px solid #e5e7eb;border-bottom:none;
  border-radius:8px 8px 0 0;font-size:.72rem;font-weight:700;color:#6b7280;letter-spacing:.02em
}
.backlog-head span{text-align:center}
.backlog-head .h-title{text-align:left}
.backlog-head .h-meta{text-align:right}
.backlog-head+.backlog-list{border-radius:0 0 8px 8px;border-top:none}
.bl-status{
  padding:3px 0;border-radius:10px;font-size:.72rem;font-weight:700;
  white-space:nowrap;text-align:center
}
.bl-status-已完成{background:#d1fae5;color:#065f46}
.bl-status-不處理不存{background:#fee2e2;color:#991b1b}
.bl-status-未知{background:#f3f4f6;color:#6b7280}
/* 素材充足度預判標註 */
.bl-assess{padding:3px 0;border-radius:10px;font-size:.72rem;font-weight:700;white-space:nowrap;text-align:center}
.bl-assess-green{background:#059669;color:#fff;box-shadow:0 0 0 2px #a7f3d0}
.bl-assess-yellow{background:#fef3c7;color:#92400e}
.bl-assess-red{background:#f3f4f6;color:#9ca3af}
.bl-assess-wait{background:#f3f4f6;color:#c4c8cf}
.backlog-row-ready{background:#ecfdf5}
.btn-import{
  padding:6px 12px;background:#2563eb;color:#fff;border:none;border-radius:6px;
  font-size:.78rem;font-weight:600;cursor:pointer;white-space:nowrap
}
.btn-import:hover{background:#1d4ed8}
.btn-import:disabled{background:#9ca3af;cursor:not-allowed;opacity:.7;pointer-events:none}

/* 資料夾瀏覽 modal */
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.42);
  display:none;align-items:center;justify-content:center;z-index:1000
}
.modal-overlay.show{display:flex}
.modal-box{
  background:#fff;border-radius:10px;width:540px;max-width:92vw;
  max-height:78vh;display:flex;flex-direction:column;
  box-shadow:0 12px 40px rgba(0,0,0,.28)
}
.modal-head{padding:14px 16px;border-bottom:1px solid #eee;display:flex;gap:8px}
.modal-head input{
  flex:1;padding:7px 10px;border:1px solid #d1d5db;border-radius:6px;
  font-size:.8rem;font-family:monospace
}
.modal-head button{
  padding:7px 14px;background:#2563eb;color:#fff;border:none;
  border-radius:6px;font-size:.82rem;cursor:pointer;white-space:nowrap
}
.modal-head button:hover{background:#1d4ed8}
.modal-body{overflow-y:auto;padding:6px 0;flex:1}
.modal-item{
  display:flex;align-items:center;gap:10px;padding:8px 18px;
  cursor:pointer;font-size:.85rem;color:#333;
  user-select:none;-webkit-user-select:none
}
.modal-item:hover{background:#f3f4f6}
.modal-item .ic{width:18px;text-align:center;flex-shrink:0}
.modal-foot{padding:10px 16px;border-top:1px solid #eee;display:flex;justify-content:space-between;align-items:center}
.modal-foot .btn-cancel{
  padding:6px 16px;background:#e5e7eb;color:#374151;border:none;
  border-radius:6px;font-size:.82rem;cursor:pointer
}
.modal-foot .btn-cancel:hover{background:#d1d5db}
.modal-foot .btn-add-sel{
  padding:6px 18px;background:#16a34a;color:#fff;border:none;
  border-radius:6px;font-size:.82rem;font-weight:600;cursor:pointer
}
.modal-foot .btn-add-sel:hover{background:#15803d}
.modal-foot .btn-add-sel:disabled{background:#bbf7d0;cursor:not-allowed}
.modal-empty{padding:24px;text-align:center;color:#999;font-size:.82rem}
.modal-item.selected{background:#eff6ff}
.modal-item .chk{
  margin-left:auto;flex-shrink:0;width:18px;height:18px;border-radius:4px;
  border:2px solid #d1d5db;display:flex;align-items:center;justify-content:center;
  font-size:.7rem;color:transparent
}
.modal-item.selected .chk{background:#2563eb;border-color:#2563eb;color:#fff}
.cat-badge{
  display:inline-block;padding:2px 10px;border-radius:12px;
  font-size:.78rem;font-weight:700
}
.cat-社會事件{background:#fef9c3;color:#713f12}
.cat-車禍{background:#fee2e2;color:#991b1b}
.cat-警匪槍戰{background:#fce7f3;color:#9d174d}
.cat-其他{background:#f3f4f6;color:#374151}
/* 統一頂部導覽 */
.topnav{display:flex;gap:6px;align-items:center;margin-bottom:22px}
.topnav h1{font-size:1.25rem;font-weight:700;margin:0 auto 0 0}
.topnav a{font-size:.83rem;color:#6b7280;text-decoration:none;padding:7px 14px;border:1px solid #d1d5db;border-radius:7px;background:#fff}
.topnav a.active{background:#2563eb;color:#fff;border-color:#2563eb}
.topnav a:hover:not(.active){background:#f9fafb}
/* 圖輯式照片素材區 */
.photo-strip{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.photo-thumb{position:relative;width:64px;height:64px;border-radius:6px;overflow:hidden;border:1px solid #e5e7eb}
.photo-thumb img{width:100%;height:100%;object-fit:cover}
.photo-thumb .rm{position:absolute;top:1px;right:1px;background:rgba(0,0,0,.6);color:#fff;border:none;border-radius:4px;font-size:.7rem;cursor:pointer;width:16px;height:16px;line-height:1;padding:0}
.btn-addphoto{padding:7px 12px;background:#fff;border:1px dashed #9ca3af;border-radius:6px;font-size:.8rem;cursor:pointer;color:#6b7280}
.btn-addphoto:hover{border-color:#2563eb;color:#2563eb}
/* 點擊查核警示定位到旁白稿：短暫紅光閃爍，跟原生選取的藍底文字一起提示位置 */
@keyframes narFlash{
  0%{box-shadow:0 0 0 0 rgba(239,68,68,.65);background:#fff5f5}
  25%{box-shadow:0 0 0 6px rgba(239,68,68,.35);background:#ffe4e4}
  50%{box-shadow:0 0 0 0 rgba(239,68,68,.65);background:#fff5f5}
  75%{box-shadow:0 0 0 6px rgba(239,68,68,.35);background:#ffe4e4}
  100%{box-shadow:0 0 0 0 rgba(239,68,68,0);background:transparent}
}
.nar-flash{animation:narFlash 1.8s ease-out;border-color:#ef4444 !important;border-width:3px !important}
#sp-nar::selection{background:#ffd24a;color:#111}
</style>
</head>
<body>
<div class="topnav">
  <h1>🎬 自由短影音產製（內部測試）</h1>
  <a href="/" class="active">產製</a>
  <a href="/gallery">成片庫</a>
  <a href="/logs">使用記錄</a>
</div>

<!-- 後臺影音清單（LTN 內部系統匯入） -->
<div class="backlog-card">
  <h2 style="margin-top:0">📡 後臺影音清單</h2>
  <div class="backlog-filters">
    <div>
      <label>起始日期</label>
      <input type="date" id="bl-start" onclick="this.showPicker && this.showPicker()">
    </div>
    <div>
      <label>結束日期</label>
      <input type="date" id="bl-end" onclick="this.showPicker && this.showPicker()">
    </div>
    <div>
      <label>狀態篩選</label>
      <select id="bl-status-filter" onchange="renderBacklog()">
        <option value="">全部</option>
        <option value="已完成">已完成</option>
        <option value="不處理不存">不處理不存</option>
      </select>
    </div>
    <button class="btn-browse" onclick="loadBacklog()">🔍 搜尋</button>
  </div>
  <div class="backlog-head">
    <span>處理狀況</span>
    <span>智慧判斷</span>
    <span class="h-title">標題</span>
    <span class="h-meta">時間 · 素材</span>
    <span>匯入</span>
  </div>
  <div class="backlog-list" id="backlog-list">
    <div style="padding:14px;color:#9ca3af;font-size:.82rem">選好日期區間後按「搜尋」載入清單</div>
  </div>
</div>

<div class="grid">

  <!-- 左：表單 -->
  <div class="card">
    <h2>輸入資料</h2>

    <label>新聞稿內容 <span style="color:#e00">*</span></label>
    <textarea id="article" placeholder="貼上新聞稿全文…"></textarea>
    <div id="import-keywords" style="display:none;margin-top:-6px;margin-bottom:12px;padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;font-size:.8rem;color:#475569;line-height:2"></div>

    <label style="margin-top:2px">🎬 影片素材</label>
    <p class="hint" style="margin-bottom:2px">從上方「後臺影音清單」按「📥 匯入」自動帶入，換匯下一則會整組換新</p>
    <p class="hint">每支匯入後自動跑智慧分析，分類與畫面描述顯示在檔名下方（分析過的有快取，不重複收費）</p>

    <div id="video-list" class="video-list"></div>

    <label style="margin-top:10px">📷 照片素材</label>
    <p class="hint" id="photo-import-note" style="display:none;color:#2563eb"></p>
    <p class="hint">沒有影片時可純用照片做「圖輯式」短影音；有影片時當補畫面備援（配不到畫面的旁白段自動用照片補，不留黑幕）</p>
    <div class="photo-strip" id="photo-strip"></div>
    <button class="btn-addphoto" onclick="document.getElementById('photo-file').click()">＋ 加照片</button>
    <input type="file" id="photo-file" accept="image/*" multiple style="display:none" onchange="uploadPhotos(this.files)">
    <p class="hint" id="photo-mode-hint" style="display:none;color:#2563eb">📸 圖輯式模式：目前沒有影片，會用這些照片輪播（Ken Burns 緩慢推移）配旁白</p>

    <div style="margin-top:14px;font-weight:400">
      <label style="display:flex;align-items:center;gap:8px">
        <span style="white-space:nowrap">🚦 停站確認：</span>
        <select id="checkpoint_mode" onchange="onCkModeChange()"
                style="flex:1;padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px;font-size:.9rem">
          <option value="smart">智慧停站（推薦）——只在有問題時才停</option>
          <option value="always">每支都停——腳本站＋分鏡站逐支把關</option>
          <option value="off">全自動——一路跑完不停（批次/過夜用）</option>
        </select>
      </label>
      <p class="hint" id="ckmode-hint" style="margin:6px 0 0;color:#6b7280"></p>
    </div>
    <label style="display:flex;align-items:center;gap:8px;margin-top:8px;font-weight:400;color:#6b7280">
      <input type="checkbox" id="lowq" checked disabled style="width:16px;height:16px">
      <span>🔒 <b>POC 低畫質模式</b>（測試期鎖定啟用，不可關）——成片庫存 480p、渲染快；正式上線再由開發端解鎖切回 1080p</span>
    </label>

    <button class="btn" id="btn" onclick="startJob()">開始產製</button>
  </div>

  <!-- 右：進度 -->
  <div class="card">
    <h2>產製進度</h2>

    <div class="progress-wrap"><div class="progress-bar" id="progress-bar"></div></div>
    <div class="progress-info">
      <span id="progress-pct">0%</span>
      <span id="progress-time">尚未開始</span>
    </div>

    <ul class="steps">
      <li id="s1"><div class="step-row"><span class="icon">○</span><span id="s1label">分析影片內容</span><span class="step-time" id="t1"></span></div><div class="step-mini"><div class="step-mini-bar" id="m1"></div></div></li>
      <li id="s2"><div class="step-row"><span class="icon">○</span>生成播報腳本（貼畫面寫）<span class="step-time" id="t2"></span></div><div class="step-mini"><div class="step-mini-bar" id="m2"></div></div></li>
      <li id="s3"><div class="step-row"><span class="icon">○</span>生成旁白音訊<span class="step-time" id="t3"></span></div><div class="step-mini"><div class="step-mini-bar" id="m3"></div></div></li>
      <li id="s4"><div class="step-row"><span class="icon">○</span>寫字幕檔<span class="step-time" id="t4"></span></div><div class="step-mini"><div class="step-mini-bar" id="m4"></div></div></li>
      <li id="s5"><div class="step-row"><span class="icon">○</span>旁白配對畫面<span class="step-time" id="t5"></span></div><div class="step-mini"><div class="step-mini-bar" id="m5"></div></div></li>
      <li id="s6"><div class="step-row"><span class="icon">○</span>冷開場（標題疊主畫面）<span class="step-time" id="t6"></span></div><div class="step-mini"><div class="step-mini-bar" id="m6"></div></div></li>
      <li id="s7"><div class="step-row"><span class="icon">○</span>組裝主畫面（長度隨旁白）<span class="step-time" id="t7"></span></div><div class="step-mini"><div class="step-mini-bar" id="m7"></div></div></li>
      <li id="s8"><div class="step-row"><span class="icon">○</span>組裝片尾（5 秒）<span class="step-time" id="t8"></span></div><div class="step-mini"><div class="step-mini-bar" id="m8"></div></div></li>
      <li id="s9"><div class="step-row"><span class="icon">○</span>串接輸出<span class="step-time" id="t9"></span></div><div class="step-mini"><div class="step-mini-bar" id="m9"></div></div></li>
    </ul>

    <!-- 旁白稿確認 checkpoint 面板（配音前，改稿成本最低的時點） -->
    <div id="script-box" style="display:none;margin-top:14px;padding:14px;border:2px solid #3b82f6;border-radius:10px;background:#eff6ff">
      <p style="font-weight:700;color:#1e40af;margin:0 0 4px">⏸ 旁白稿確認（配音前）</p>
      <p style="font-size:.78rem;color:#1e3a8a;margin:0 0 10px">先看稿再配音：稿不對就直接改（或取消重跑），省下後面配音＋配畫面的時間。查核與譯名結果已列出。</p>
      <div id="sp-content" style="font-size:.82rem;color:#333"></div>
      <div style="margin-top:8px"><b style="font-size:.82rem">旁白全文（可直接修改）：</b>
        <textarea id="sp-nar" style="width:100%;min-height:130px;margin-top:4px;padding:8px;border:1px solid #cbd5e1;border-radius:6px;font-size:.85rem;line-height:1.7;font-family:inherit"></textarea>
      </div>
      <div style="display:flex;gap:10px;margin-top:12px">
        <button class="btn" style="flex:1;background:#16a34a" onclick="confirmScript('go')">✅ 稿 OK，開始配音</button>
        <button class="btn" style="flex:1;background:#9ca3af" onclick="confirmScript('cancel')">✖ 取消</button>
      </div>
    </div>

    <!-- 分鏡確認 checkpoint 面板 -->
    <div id="checkpoint-box" style="display:none;margin-top:14px;padding:14px;border:2px solid #f59e0b;border-radius:10px;background:#fffbeb">
      <p style="font-weight:700;color:#92400e;margin:0 0 4px">⏸ 渲染前分鏡確認</p>
      <p style="font-size:.78rem;color:#78350f;margin:0 0 10px">下面是這支的旁白、Hook、每段配到的畫面與字幕。確認沒問題再渲染；覺得不對就取消、調整後重跑（省下整支渲染時間）。</p>
      <div id="cp-content" style="font-size:.82rem;color:#333"></div>
      <div id="draft-area" style="margin-top:10px"></div>
      <div style="display:flex;gap:10px;margin-top:14px">
        <button class="btn" style="flex:1;background:#0ea5e9" onclick="draftPreview()" id="draft-btn">🎬 快速預覽（低畫質）</button>
        <button class="btn" style="flex:1;background:#16a34a" onclick="confirmJob('go')">✅ 確認，開始渲染</button>
        <button class="btn" style="flex:1;background:#9ca3af" onclick="confirmJob('cancel')">✖ 取消</button>
      </div>
    </div>

    <div class="result" id="result">
      <p>✅ 產製完成！《<span id="titleText"></span>》</p>
      <video id="preview-video" controls style="width:100%;border-radius:8px;margin-bottom:10px;background:#000"></video>
      <div id="plan-box" style="display:none;margin-bottom:10px">
        <p style="font-size:.8rem;color:#374151;font-weight:600;margin-bottom:6px">🎞 旁白配對畫面明細</p>
        <div id="plan-rows" style="font-size:.76rem;color:#555;font-family:monospace;line-height:1.7"></div>
      </div>
      <div id="hashtag-box" style="display:none;margin-bottom:10px">
        <p style="font-size:.8rem;color:#374151;font-weight:600;margin-bottom:6px">🏷 上架標籤（點擊複製）</p>
        <div id="hashtag-text" onclick="copyHashtags()"
             style="font-size:.82rem;color:#2563eb;cursor:pointer;padding:8px 10px;background:#f3f4f6;border-radius:6px;word-break:break-all"></div>
      </div>
      <a class="dl" id="dl" href="#">⬇ 下載成片</a>
    </div>
    <div class="warn" id="warn"></div>
    <div class="err" id="err"></div>
  </div>

</div>

<!-- 資料夾瀏覽 modal -->
<div class="modal-overlay" id="browse-modal">
  <div class="modal-box">
    <div class="modal-head">
      <input type="text" id="browse-path" placeholder="D:\\VideoAI\\input">
      <button onclick="browseGo()">前往</button>
    </div>
    <div style="padding:6px 16px;font-size:.74rem;color:#999;border-bottom:1px solid #f3f4f6">
      點擊勾選；先點第一個，再按住 Shift 點最後一個，可一次全選中間所有檔案
    </div>
    <div class="modal-body" id="browse-list"></div>
    <div class="modal-foot">
      <button class="btn-cancel" onclick="closeBrowse()">取消</button>
      <button class="btn-add-sel" id="btn-add-sel" onclick="addSelectedToList()" disabled>加入選取（0）</button>
    </div>
  </div>
</div>

<script>
let timer = null;

async function startJob() {
  const art = document.getElementById('article').value.trim();
  if (!art) { alert('請填入新聞稿'); return; }

  saveLastSettings();
  resetUI();
  document.getElementById('btn').disabled = true;

  const videos = videoList.map(v => ({path: v.path, start: v.start}));

  try {
    const r = await fetch('/api/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        article: art,
        videos:  videos,
        photos:  importedPhotos,
        checkpoint_mode: document.getElementById('checkpoint_mode').value,
        low_quality: document.getElementById('lowq').checked,
      })
    });
    const d = await r.json();
    if (d.error) { showErr(d.error); document.getElementById('btn').disabled = false; return; }
    timer = setInterval(poll, 1000);
  } catch(e) {
    showErr(e.message);
    document.getElementById('btn').disabled = false;
  }
}

const STEP_NAMES = ['', '分析影片內容', '生成播報腳本', '生成旁白音訊', '寫字幕檔',
                     '旁白配對畫面',
                     '冷開場（無片頭卡）', '組裝主畫面', '組裝片尾（2.8秒）', '串接輸出'];
const N_STEPS = 9;
let cpShown = false;     // 分鏡確認面板是否已顯示（避免每次輪詢重抓 preview）
let cpDecided = false;   // 使用者已按過確認/取消（避免 server 尚未清旗標時面板閃回）
let spShown = false;     // 旁白稿確認面板（Checkpoint A）
let spDecided = false;

async function poll() {
  try {
    const d = await (await fetch('/api/status')).json();
    renderSteps(d);
    if (d.awaiting_script && !spDecided) {
      if (!spShown) { spShown = true; showScriptCheckpoint(); }
      return;   // 等旁白稿確認
    }
    if (spShown && !d.awaiting_script) {
      spShown = false;
      document.getElementById('script-box').style.display = 'none';
    }
    if (d.awaiting_confirm && !cpDecided) {
      if (!cpShown) { cpShown = true; showCheckpoint(); }
      return;   // 暫停中，等使用者按確認/取消，不當作完成
    }
    if (cpShown && !d.awaiting_confirm) {
      // 已確認、繼續渲染 → 收起面板
      cpShown = false;
      document.getElementById('checkpoint-box').style.display = 'none';
    }
    if (d.done) {
      clearInterval(timer);
      document.getElementById('btn').disabled = false;
      handleJobDone(d);
    }
  } catch(_) {}
}

async function showScriptCheckpoint() {
  try {
    const pv = await (await fetch('/api/script_preview')).json();
    renderScriptCheckpoint(pv);
    document.getElementById('script-box').style.display = 'block';
    document.getElementById('script-box').scrollIntoView({behavior:'smooth', block:'nearest'});
  } catch(_) {}
}

function renderScriptCheckpoint(pv) {
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  let html = '';
  // 查核／譯名警示沿用分鏡面板的樣式（在改稿階段看到最有用）
  spFactcheckIssues = pv.factcheck || [];
  if (pv.factcheck && pv.factcheck.length) {
    // 分兩類：矛盾/捏造＝真的會出事，紅色醒目；省略＝旁白比原稿少講但不衝突，灰色小字
    const isOmit = f => (f.type === '省略') || (f.type == null && f.severity === 'low');
    const bad = [], omit = [];
    pv.factcheck.forEach((f, idx) => (isOmit(f) ? omit : bad).push([f, idx]));
    if (bad.length) {
      const rows = bad.map(([f, idx]) =>
        `<div onclick="jumpToNarrationIssue(${idx})" title="點擊定位到下方旁白區"
          style="margin:4px 0;padding:6px 8px;background:#fff;border-radius:5px;cursor:pointer;display:flex;gap:6px;align-items:baseline">
          <span style="flex:1"><span style="color:#b91c1c;font-weight:700">●${esc(f.type||'矛盾')}</span> <b>${esc(f.field)}</b>：旁白說「${esc(f.narration_says)}」，但原稿是「${esc(f.article_says)}」</span>
          <span style="color:#2563eb;font-size:.74rem;white-space:nowrap">📍 定位</span></div>`).join('');
      html += `<div style="margin-bottom:10px;padding:10px 12px;background:#fef2f2;border:2px solid #fca5a5;border-radius:8px">
        <div style="color:#991b1b;font-weight:700;margin-bottom:6px">🚨 事實查核：${bad.length} 處與原稿牴觸——務必核對！點警示可定位到下面稿子</div>
        ${rows}</div>`;
    }
    if (omit.length) {
      const rows = omit.map(([f, idx]) =>
        `<div onclick="jumpToNarrationIssue(${idx})" title="點擊定位到下方旁白區"
          style="margin:3px 0;cursor:pointer;color:#6b7280">・<b>${esc(f.field)}</b>：旁白省略了「${esc(f.article_says)}」（不衝突，視版面可不補）</div>`).join('');
      html += `<div style="margin-bottom:10px;padding:8px 12px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;font-size:.78rem">
        <div style="color:#6b7280;font-weight:700;margin-bottom:4px">ℹ️ 省略提醒（${omit.length} 處，非錯誤）：旁白比原稿少講的細節，通常可接受</div>
        ${rows}</div>`;
    }
  }
  if (pv.translit && pv.translit.length) {
    const mis = pv.translit.filter(t => t.status === 'mismatch');
    if (mis.length) {
      html += `<div style="margin-bottom:10px;padding:8px 12px;background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;font-size:.8rem">
        🈯 譯名：${mis.map(t=>`「<b>${esc(t.chinese)}</b>」應為「<b style="color:#166534">${esc(t.expected)}</b>」`).join('；')}</div>`;
    }
  }
  html += `<div style="margin-bottom:6px"><b>標題：</b>${esc(pv.title)}　<b>Hook：</b>${esc(pv.hook)}</div>`;
  html += `<div style="margin-bottom:6px"><b>Hashtags：</b>${esc((pv.hashtags||[]).join(' ')||'（無）')}</div>`;
  if (pv.sots && pv.sots.length) {
    html += `<div style="margin-bottom:6px;padding:8px 10px;background:#fff;border-radius:6px">
      <b>🎤 受訪原音安排（${pv.sots.length} 段）：</b>${pv.sots.map(s =>
        `<div style="margin-top:4px;font-size:.8rem;color:#444">${esc(s.label)}・${s.dur}s：「${esc(s.display)}」${s.speaker?`<div style="color:#1d4ed8">📛 名條：${esc(s.speaker)}</div>`:`<div style="color:#92400e">（無法確認發言者，不掛名條）</div>`}</div>`).join('')}</div>`;
  }
  if (pv.card && pv.card.type) {
    const c = pv.card;
    const names = {stat:'數據卡', timeline:'時序卡', alert:'警示卡', chart:'圖表卡'};
    let desc = '';
    if (c.type === 'stat') desc = `${esc(c.label||'')} <b>${esc(c.value||'')}${esc(c.unit||'')}</b>`;
    else if (c.type === 'timeline') desc = `${esc(c.title||'')}：${(c.steps||[]).map(esc).join(' → ')}`;
    else if (c.type === 'alert') desc = `<b>${esc(c.headline||'')}</b>　${esc(c.sub||'')}`;
    else if (c.type === 'chart') desc = `${esc(c.title||'')}（${(c.points||[]).map(p=>esc(p.label)+' '+esc(String(p.value))).join('、')}）`;
    html += `<div style="margin-bottom:6px;padding:8px 10px;background:#fff;border-radius:6px;font-size:.8rem;display:flex;align-items:center;gap:10px">
      <span style="flex:1">📊 ${names[c.type]||'資訊卡'}：${desc}</span>
      <label style="display:flex;align-items:center;gap:5px;white-space:nowrap;cursor:pointer;color:#6b7280">
        <input type="checkbox" id="sp-usecard" checked style="width:15px;height:15px">使用這張卡
      </label></div>`;
  }
  if (pv.highlights && pv.highlights.length) {
    html += `<div style="margin-bottom:6px;padding:8px 10px;background:#fff;border-radius:6px;font-size:.8rem">
      🖍 字幕標色關鍵詞：${pv.highlights.map(h=>`<span style="background:#fef08a;padding:1px 6px;border-radius:4px;margin-right:4px">${esc(h)}</span>`).join('')}（數字一律自動標色）</div>`;
  }
  document.getElementById('sp-content').innerHTML = html;
  document.getElementById('sp-nar').value = pv.narration || '';
}

async function confirmScript(action) {
  const box = document.getElementById('script-box');
  box.querySelectorAll('button').forEach(b => b.disabled = true);
  spDecided = true;
  try {
    const useCard = document.getElementById('sp-usecard');
    await fetch('/api/confirm_script', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, narration: document.getElementById('sp-nar').value,
                            drop_card: !!(useCard && !useCard.checked)}),
    });
  } catch(_) {}
  box.querySelectorAll('button').forEach(b => b.disabled = false);
  spShown = false;
  box.style.display = 'none';
}

async function showCheckpoint() {
  try {
    const pv = await (await fetch('/api/preview')).json();
    renderCheckpoint(pv);
    document.getElementById('checkpoint-box').style.display = 'block';
    document.getElementById('checkpoint-box').scrollIntoView({behavior:'smooth', block:'nearest'});
  } catch(_) {}
}

let planEdits = {};      // 分鏡站的人工換片段 {rowIndex: {index, path, start}}
let planRemovals = new Set();   // 分鏡站要移除的資訊卡 row index
let pvOptions = [];      // 素材庫全部片段（換片段下拉的資料來源）
let spFactcheckIssues = [];   // 旁白稿確認站的查核清單（點擊定位用）

function toast(m) {
  let t = document.getElementById('toast2');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast2';
    t.style.cssText = 'position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#111;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;z-index:400;opacity:0;transition:opacity .3s;pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent = m; t.style.opacity = '1';
  setTimeout(() => t.style.opacity = '0', 2200);
}

// 在旁白（可能已被人工修改過的 textarea 內容）裡找出查核片段的位置。
// 查核給的 narration_says 常夾帶自己的註解（如「（未交代罰鍰區間）」）或經過改寫，
// 未必逐字存在於旁白裡，所以純 indexOf 常找不到。改成分層比對：
//   ① 整串直接命中 ② 去掉括號註解再試 ③ 退回「旁白裡真的有的最長連續片段」
function _locateInNarration(hay, needle) {
  if (!needle) return null;
  let i = hay.indexOf(needle);
  if (i >= 0) return [i, needle.length];
  const stripped = needle.replace(/[（(][^）)]*[）)]/g, '').trim();
  if (stripped && stripped !== needle) {
    i = hay.indexOf(stripped);
    if (i >= 0) return [i, stripped.length];
  }
  // 最長連續共同片段（滑動視窗，從長到短找 needle 的哪一段確實在旁白裡）
  const n = stripped || needle;
  for (let len = n.length; len >= 6; len--) {
    for (let s = 0; s + len <= n.length; s++) {
      const frag = n.slice(s, s + len);
      const p = hay.indexOf(frag);
      if (p >= 0) return [p, len];
    }
  }
  return null;
}

function jumpToNarrationIssue(idx) {
  const f = spFactcheckIssues[idx];
  const ta = document.getElementById('sp-nar');
  if (!f || !ta) return;
  const hit = _locateInNarration(ta.value, f.narration_says || '');
  ta.scrollIntoView({behavior: 'smooth', block: 'center'});
  if (!hit) {
    toast('在旁白裡找不到對應文字（可能已被改寫）');
    return;
  }
  const [pos, len] = hit;
  // 依命中行數估算捲動位置，讓命中片段出現在 textarea 可視範圍中央
  const lineNo = ta.value.slice(0, pos).split('\\n').length - 1;
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 26;
  ta.focus();
  ta.setSelectionRange(pos, pos + len);   // 選取＝持續醒目，改稿完才消失
  ta.scrollTop = Math.max(0, lineNo * lineHeight - ta.clientHeight / 2);
  ta.classList.remove('nar-flash');
  void ta.offsetWidth;   // 強制 reflow，讓動畫能重新觸發（連續點同一項也要再閃一次）
  ta.classList.add('nar-flash');
  setTimeout(() => ta.classList.remove('nar-flash'), 1800);
}

function toggleRemoveCard(i) {
  const row = document.getElementById('cprow' + i);
  if (planRemovals.has(i)) {
    planRemovals.delete(i);
    row.style.opacity = '';
    row.style.textDecoration = '';
    document.getElementById('rmbtn' + i).textContent = '✖ 移除這張卡';
  } else {
    planRemovals.add(i);
    row.style.opacity = '0.45';
    row.style.textDecoration = 'line-through';
    document.getElementById('rmbtn' + i).textContent = '↩ 復原';
  }
}

function escH(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function toggleSwap(i) {
  const d = document.getElementById('swap' + i);
  if (d.style.display === 'none') {
    if (!d.innerHTML) {
      const opts = pvOptions.map((o, j) =>
        `<option value="${j}">${escH(o.name)}　${o.start}~${o.end}s　${escH(o.desc)}</option>`).join('');
      d.innerHTML = `<select onchange="applySwap(${i}, this.value)" style="width:100%;font-size:.74rem;padding:5px;border:1px solid #cbd5e1;border-radius:5px">
        <option value="">— 選擇要換成哪個片段（維持原時長）—</option>${opts}</select>`;
    }
    d.style.display = 'block';
  } else {
    d.style.display = 'none';
  }
}

function applySwap(i, j) {
  const row = document.getElementById('cprow' + i);
  const label = document.getElementById('swaplabel' + i);
  if (j === '') {
    delete planEdits[i];
    row.style.outline = '';
    label.textContent = '';
    return;
  }
  const o = pvOptions[Number(j)];
  planEdits[i] = {index: i, path: o.path, start: o.start};
  row.style.outline = '2px solid #2563eb';
  label.textContent = '👤 已改選：' + o.name + ' 第' + o.start + 's 起';
}

function renderCheckpoint(pv) {
  planEdits = {};
  planRemovals = new Set();
  pvOptions = pv.options || [];
  document.getElementById('draft-area').innerHTML = '';
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const tags = (pv.hashtags && pv.hashtags.length) ? pv.hashtags.join(' ') : '（無）';
  let html = '';
  // ① 為什麼停在這裡（智慧停站理由）——擺最上面，一眼看到重點
  if (pv.stop_reasons && pv.stop_reasons.length) {
    html += `<div style="margin-bottom:12px;padding:8px 12px;background:#fffbeb;border:2px solid #fcd34d;border-radius:8px;font-size:.82rem">
      <b style="color:#92400e">🚦 這支停下來讓你看，是因為：</b> ${pv.stop_reasons.map(esc).join('、')}</div>`;
  }
  // ② 查核 & 譯名：有問題才紅框攤開；都沒問題只給一行綠字（不再洗版）
  const isOmit = f => (f.type === '省略') || (f.type == null && f.severity === 'low');
  const bad = (pv.factcheck || []).filter(f => !isOmit(f));
  const omit = (pv.factcheck || []).filter(isOmit);
  const mis = (pv.translit || []).filter(t => t.status === 'mismatch');
  const std = (pv.translit || []).filter(t => t.status === 'standard');
  const unk = (pv.translit || []).filter(t => t.status === 'unknown');
  if (bad.length) {
    const rows = bad.map(f =>
      `<div style="margin:4px 0;padding:6px 8px;background:#fff;border-radius:5px">
        <span style="color:#b91c1c;font-weight:700">●${esc(f.type||'矛盾')}</span> <b>${esc(f.field)}</b>：旁白說「${esc(f.narration_says)}」，但原稿是「${esc(f.article_says)}」</div>`).join('');
    html += `<div style="margin-bottom:10px;padding:10px 12px;background:#fef2f2;border:2px solid #fca5a5;border-radius:8px">
      <div style="color:#991b1b;font-weight:700;margin-bottom:6px">🚨 事實查核：${bad.length} 處與原稿牴觸，請務必核對後再渲染</div>
      ${rows}</div>`;
  }
  if (mis.length) {
    const rows = mis.map(t =>
      `<div style="margin:3px 0;padding:5px 8px;background:#fff;border-radius:5px">
        <span style="color:#b91c1c;font-weight:700">✗</span> 旁白用「<b>${esc(t.chinese)}</b>」，報社標準譯名是「<b style="color:#166534">${esc(t.expected)}</b>」（${esc(t.english)}）</div>`).join('');
    html += `<div style="margin-bottom:10px;padding:10px 12px;background:#fef2f2;border:2px solid #fca5a5;border-radius:8px">
      <div style="color:#991b1b;font-weight:700;margin-bottom:4px">🈯 譯名核實：${mis.length} 處與音譯表不符</div>${rows}</div>`;
  }
  if (!bad.length && !mis.length) {
    html += `<div style="margin-bottom:10px;padding:7px 12px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;color:#166534;font-size:.82rem">✓ 事實查核與譯名皆無誤</div>`;
  }
  // ③ 標題 / Hook（可直接改）
  html += `<div style="display:flex;gap:12px;margin-bottom:6px">
    <div style="flex:1;min-width:0">
      <b style="display:block;font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px">🏷 開場標題字卡 <span style="color:#94a3b8;font-weight:400">（可改）</span></b>
      <input id="cp-title" value="${esc(pv.title)}" style="width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #cbd5e1;border-radius:6px;font-size:.88rem"></div>
    <div style="flex:1;min-width:0">
      <b style="display:block;font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px">⚡ Hook 字卡 <span style="color:#94a3b8;font-weight:400">（可改）</span></b>
      <input id="cp-hook" value="${esc(pv.hook)}" style="width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #cbd5e1;border-radius:6px;font-size:.88rem"></div>
  </div>`;
  // ④ 分鏡縮圖（本站主體，保留逐段換畫面）
  if (pv.segments && pv.segments.length) {
    html += `<div style="margin:8px 0 6px"><b>配對畫面（每段配到什麼）：</b><span style="font-size:.74rem;color:#92400e">　配錯 → 按「🔄 換」改選素材</span></div>`;
    let at = 0;
    html += pv.segments.map((s, i) => {
      const from = at.toFixed(0); at += (s.dur || 0); const to = at.toFixed(0);
      const img = s.thumb
        ? `<img src="data:image/jpeg;base64,${s.thumb}" style="width:90px;border-radius:6px;flex-shrink:0">`
        : `<div style="width:90px;height:60px;background:#111;border-radius:6px;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#888;font-size:.7rem">黑幕</div>`;
      const swapBtn = s.editable
        ? `<button onclick="toggleSwap(${i})" style="margin-left:8px;font-size:.7rem;padding:1px 8px;border:1px solid #d1d5db;border-radius:5px;background:#fff;cursor:pointer">🔄 換</button>`
        : (s.removable
          ? `<button id="rmbtn${i}" onclick="toggleRemoveCard(${i})" style="margin-left:8px;font-size:.7rem;padding:1px 8px;border:1px solid #fca5a5;border-radius:5px;background:#fff;color:#b91c1c;cursor:pointer">✖ 移除這張卡</button>`
          : '');
      return `<div id="cprow${i}" style="margin-bottom:8px;background:#fff;padding:6px;border-radius:6px">
        <div style="display:flex;gap:10px;align-items:center">
          ${img}
          <div style="font-size:.76rem;line-height:1.5;min-width:0">
            <div style="color:#92400e;font-weight:600">${from}-${to}s ← ${esc(s.video)}${s.start ? ' 第'+s.start+'s起' : ''}${swapBtn}</div>
            <div style="color:#666">${esc(s.why)}</div>
            <div id="swaplabel${i}" style="color:#1d4ed8;font-weight:600"></div>
          </div>
        </div>
        <div id="swap${i}" style="display:none;margin-top:6px"></div>
      </div>`;
    }).join('');
  }
  // ⑤ 次要文字資訊收進摺疊（預設收起，要看才展開）——這是「畫面太雜」的主因，全部藏起來
  const omitHtml = omit.length
    ? `<div style="margin-top:6px"><b style="color:#6b7280">省略提醒（${omit.length} 處，非錯誤）：</b>${omit.map(f=>`<div style="margin:2px 0;color:#6b7280">・<b>${esc(f.field)}</b>：省略了「${esc(f.article_says)}」</div>`).join('')}</div>`
    : '';
  const trOkHtml = (std.length || unk.length)
    ? `<div style="margin-top:6px">${std.length?`<span style="color:#166534">✓ 譯名符合標準：${std.map(t=>esc(t.chinese)).join('、')}</span>`:''}${unk.length?`　<span style="color:#92400e">ℹ 音譯表未收錄：${unk.map(t=>esc(t.chinese)).join('、')}</span>`:''}</div>`
    : '';
  const subsHtml = (pv.subtitles && pv.subtitles.length)
    ? `<div style="margin-bottom:8px"><b>字幕分段（${pv.subtitles.length} 句）：</b><div style="color:#555;margin-top:4px;line-height:1.8">${pv.subtitles.map(esc).join(' ／ ')}</div></div>`
    : '';
  html += `<details style="margin-top:10px;font-size:.82rem">
    <summary style="cursor:pointer;color:#2563eb;font-weight:600;user-select:none">▸ 看文字細節（旁白全文／字幕／Hashtags${omit.length?'／省略提醒':''}）</summary>
    <div style="margin-top:8px">
      <div style="margin-bottom:8px"><b>旁白全文：</b><div style="white-space:pre-wrap;line-height:1.7;margin-top:4px;background:#fff;padding:8px;border-radius:6px">${esc(pv.narration)}</div></div>
      ${subsHtml}
      <div><b>Hashtags：</b>${esc(tags)}</div>
      ${omitHtml}${trOkHtml}
    </div>
  </details>`;
  document.getElementById('cp-content').innerHTML = html;
}

async function draftPreview() {
  // 用目前分鏡（含尚未確認的換片段）渲染 480p 草稿：字幕/名條/數據卡/BGM 全都在
  const area = document.getElementById('draft-area');
  const btn = document.getElementById('draft-btn');
  btn.disabled = true;
  area.innerHTML = '<div style="font-size:.8rem;color:#92400e">⏳ 預覽渲染中（約 10~20 秒）…</div>';
  try {
    const r = await fetch('/api/draft', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({plan_edits: Object.values(planEdits),
        plan_removals: Array.from(planRemovals),
        title: (document.getElementById('cp-title')||{}).value||'',
        hook: (document.getElementById('cp-hook')||{}).value||''}),
    });
    const d = await r.json();
    if (d.error) { area.innerHTML = '<div style="color:#b91c1c;font-size:.8rem">預覽失敗：' + d.error + '</div>'; btn.disabled = false; return; }
    const timer = setInterval(async () => {
      const s = await (await fetch('/api/draft_status')).json();
      if (s.running) return;
      clearInterval(timer);
      btn.disabled = false;
      if (s.error || !s.ready) {
        area.innerHTML = '<div style="color:#b91c1c;font-size:.8rem">預覽失敗：' + (s.error || '未知原因') + '</div>';
        return;
      }
      area.innerHTML =
        '<video controls playsinline autoplay muted style="width:100%;max-height:430px;border-radius:8px;background:#000" src="/api/draft_video?ts=' + Date.now() + '"></video>' +
        '<div style="font-size:.74rem;color:#92400e;margin-top:4px">此為低畫質預覽（480p）；正式渲染為完整畫質。換了片段可再按一次預覽。</div>';
    }, 1500);
  } catch(e) {
    area.innerHTML = '<div style="color:#b91c1c;font-size:.8rem">預覽失敗：' + e.message + '</div>';
    btn.disabled = false;
  }
}

async function confirmJob(action) {
  const box = document.getElementById('checkpoint-box');
  box.querySelectorAll('button').forEach(b => b.disabled = true);
  cpDecided = true;   // 先擋住 poll 再送出，避免 server 尚未清旗標時面板閃回
  try {
    await fetch('/api/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, plan_edits: Object.values(planEdits),
        plan_removals: Array.from(planRemovals),
        title: (document.getElementById('cp-title')||{}).value||'',
        hook: (document.getElementById('cp-hook')||{}).value||''}),
    });
  } catch(_) {}
  box.querySelectorAll('button').forEach(b => b.disabled = false);
  cpShown = false;
  box.style.display = 'none';
  // 之後由 poll 繼續：'go' 會看到渲染步驟、'cancel' 會走到 done+cancelled
}

function handleJobDone(d) {
  // 記下這支工作「已經看過結果了」，避免下次開網頁又跳出同一支的完成畫面
  if (d.started_at) localStorage.setItem('videoai_last_seen_job', String(d.started_at));
  if (d.error) {
    showErr(d.error);
  } else if (d.cancelled) {
    showErr('已取消：分鏡未通過，未進行渲染。調整後可重新產製。');
  } else if (d.filename) {
    showResult(d.filename, d.title, d.warning, d.plan, d.hashtags);
    // 影片清單保留（方便核對這次用了哪些素材），要換下一則新聞才手動按「重整」清空
  }
}

// 換分頁/視窗、或重新整理頁面後，自動接上仍在跑（或剛跑完還沒看過結果）的工作；
// 已經看過結果的舊工作不會重複跳出來
function syncInputsFromJob(d) {
  // 接上進行中的工作（含重跑）時，把左側清單/文稿/照片換成「這個工作實際用的輸入」
  // 文稿獨立同步——不能因為影片清單剛好一致就連文稿一起跳過（踩過：影片有貼回、文章沒貼回）
  if (d.input_article) {
    const art = document.getElementById('article');
    if (art.value.trim() !== d.input_article.trim()) art.value = d.input_article;
  }
  if (!d.input_videos || !d.input_videos.length) return;
  const cur = videoList.map(v => v.path).join(';');
  if (cur === d.input_videos.join(';')) return;   // 清單已一致就不動
  videoList = d.input_videos.map(p => ({path: p, start: 0, analyzing: false, analysis: null}));
  importedPhotos = d.input_photos || [];
  renderVideoList();
  renderPhotoStrip();
  analyzeQueued(videoList.slice());   // 分析有快取，只是把分類/描述補回畫面
}

async function resumeIfRunning() {
  try {
    const d = await (await fetch('/api/status')).json();
    if (!d.done) {
      document.getElementById('btn').disabled = true;
      syncInputsFromJob(d);
      renderSteps(d);
      timer = setInterval(poll, 1000);
    } else if ((d.filename || d.error) && d.started_at &&
               String(d.started_at) !== localStorage.getItem('videoai_last_seen_job')) {
      renderSteps(d);
      handleJobDone(d);
    }
  } catch(_) {}
}

// 各步驟的「典型耗時」(秒)，用來把進行中步驟的細進度條推到接近滿（但不到 100%）
const STEP_TYPICAL = {1:15, 2:14, 3:5, 4:0.5, 5:7, 6:1, 7:25, 8:1, 9:0.5};

function renderSteps(d) {
  // 步驟 1 執行中會帶「（1/3）」這種進度，動態顯示
  if (d.step === 1 && d.msg && d.msg.indexOf(STEP_NAMES[1]) === 0) {
    document.getElementById('s1label').textContent = d.msg;
  } else {
    document.getElementById('s1label').textContent = STEP_NAMES[1];
  }

  for (let i = 1; i <= N_STEPS; i++) {
    const el = document.getElementById('s' + i);
    const icon = el.querySelector('.icon');
    const timeEl = document.getElementById('t' + i);
    const miniBar = document.getElementById('m' + i);
    const dur = d.step_durations && d.step_durations[STEP_NAMES[i]];

    if (i < d.step) {
      // 已完成的步驟
      el.className = 'done'; icon.textContent = '✅';
      timeEl.textContent = dur != null ? dur.toFixed(1) + 's' : '';
      timeEl.classList.remove('running');
      miniBar.style.width = '100%';
    } else if (i === d.step) {
      const finished = d.done && !d.error;
      el.className = finished ? 'done' : 'active';
      icon.textContent = finished ? '✅' : '⏳';
      if (finished) {
        timeEl.textContent = dur != null ? dur.toFixed(1) + 's' : '';
        timeEl.classList.remove('running');
        miniBar.style.width = '100%';
      } else {
        // 執行中：秒數紅色，細進度條依「已耗時 / 典型耗時」推進（上限 92%，避免假裝跑完）
        const elapsed = d.step_elapsed_sec != null ? d.step_elapsed_sec : 0;
        timeEl.textContent = elapsed.toFixed(1) + 's';
        timeEl.classList.add('running');
        const typical = STEP_TYPICAL[i] || 10;
        const pct = Math.min(92, (elapsed / typical) * 100);
        miniBar.style.width = pct + '%';
      }
    } else {
      // 還沒到的步驟
      el.className = ''; icon.textContent = '○';
      timeEl.textContent = '';
      timeEl.classList.remove('running');
      miniBar.style.width = '0%';
    }
  }
  renderProgress(d);
}

function renderProgress(d) {
  const pct = Math.round((Math.min(d.step, N_STEPS) / N_STEPS) * 100);
  document.getElementById('progress-bar').style.width = pct + '%';
  document.getElementById('progress-pct').textContent = pct + '%';
  const timeEl = document.getElementById('progress-time');
  const t = d.elapsed_sec != null ? d.elapsed_sec.toFixed(1) : '0.0';
  if (d.done) timeEl.textContent = (d.error ? '中斷於 ' : '完成，共 ') + t + ' 秒';
  else        timeEl.textContent = '已執行 ' + t + ' 秒';
}

function showResult(fname, title, warning, plan, hashtags) {
  document.getElementById('titleText').textContent = title || '';
  const dlUrl = '/api/download/' + encodeURIComponent(fname);
  document.getElementById('dl').href = dlUrl;
  document.getElementById('dl').setAttribute('download', fname);
  const video = document.getElementById('preview-video');
  video.src = dlUrl;
  document.getElementById('result').style.display = 'block';

  // 配對明細
  const planBox = document.getElementById('plan-box');
  const planRows = document.getElementById('plan-rows');
  if (plan && plan.length) {
    let at = 0;
    planRows.innerHTML = plan.map(e => {
      const from = at.toFixed(0);
      at += e.dur;
      const to = at.toFixed(0);
      const src = e.video === '（黑幕）'
        ? '<span style="color:#999">（黑幕）</span>'
        : `${e.video} ${e.start}s起`;
      const why = e.why ? `<span style="color:#999"> — ${e.why}</span>` : '';
      return `${from}-${to}s ← ${src}${why}`;
    }).join('<br>');
    planBox.style.display = 'block';
  } else {
    planBox.style.display = 'none';
  }

  const warnEl = document.getElementById('warn');
  if (warning) {
    warnEl.textContent = '⚠️ ' + warning;
    warnEl.style.display = 'block';
  } else {
    warnEl.style.display = 'none';
  }

  const hashtagBox = document.getElementById('hashtag-box');
  if (hashtags && hashtags.length) {
    document.getElementById('hashtag-text').textContent = hashtags.join(' ');
    hashtagBox.style.display = 'block';
  } else {
    hashtagBox.style.display = 'none';
  }
}

function copyHashtags() {
  const text = document.getElementById('hashtag-text').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const el = document.getElementById('hashtag-text');
    const orig = el.textContent;
    el.textContent = '已複製 ✓ ' + orig;
    setTimeout(() => { el.textContent = orig; }, 1200);
  });
}

function showErr(msg) {
  const el = document.getElementById('err');
  el.textContent = '❌ ' + msg;
  el.style.display = 'block';
}

function resetUI() {
  document.getElementById('result').style.display = 'none';
  document.getElementById('err').style.display = 'none';
  document.getElementById('warn').style.display = 'none';
  document.getElementById('plan-box').style.display = 'none';
  const cp = document.getElementById('checkpoint-box');
  if (cp) cp.style.display = 'none';
  const sp = document.getElementById('script-box');
  if (sp) sp.style.display = 'none';
  cpShown = false;
  cpDecided = false;
  spShown = false;
  spDecided = false;
  document.getElementById('preview-video').removeAttribute('src');
  document.getElementById('progress-bar').style.width = '0%';
  document.getElementById('progress-pct').textContent = '0%';
  document.getElementById('progress-time').textContent = '開始處理…';
  for (let i = 1; i <= N_STEPS; i++) {
    const el = document.getElementById('s' + i);
    el.className = '';
    el.querySelector('.icon').textContent = '○';
    const timeEl = document.getElementById('t' + i);
    timeEl.textContent = '';
    timeEl.classList.remove('running');
    document.getElementById('m' + i).style.width = '0%';
  }
}

// ─── 多影片清單（含每支自動智慧分析）─────────────────────────────────────────
let videoList = [];  // {path, start, analyzing, analysis:{category,description,error}}
let importedPhotos = [];  // 匯入時下載的新聞配圖本機路徑（配不到畫面時當 Ken Burns 備援）

function removeVideoFromList(index) {
  videoList.splice(index, 1);
  renderVideoList();
  saveLastSettings();
}

function clearVideoList() {
  if ((videoList.length || importedPhotos.length) && !confirm('確定要清空目前的影片與照片清單嗎？')) return;
  videoList = [];
  importedPhotos = [];
  document.getElementById('import-keywords').style.display = 'none';
  renderVideoList();
  renderPhotoStrip();
  saveLastSettings();
}

// ─── 照片素材（圖輯式模式）──────────────────────────────────────────────────
function renderPhotoStrip() {
  const strip = document.getElementById('photo-strip');
  strip.innerHTML = importedPhotos.map((p, i) =>
    `<div class="photo-thumb">
       <img src="/api/photo?path=${encodeURIComponent(p)}" alt="" loading="lazy"
            style="cursor:zoom-in" onclick="showPhotoLarge(${i})">
       <button class="rm" onclick="removePhoto(${i})" title="移除">&times;</button>
     </div>`).join('');
  // 沒影片但有照片 → 圖輯式模式提示
  const hint = document.getElementById('photo-mode-hint');
  hint.style.display = (!videoList.length && importedPhotos.length) ? 'block' : 'none';
}

function removePhoto(i) {
  importedPhotos.splice(i, 1);
  renderPhotoStrip();
  saveLastSettings();
}

async function uploadPhotos(files) {
  for (const f of files) {
    const fd = new FormData();
    fd.append('photo', f);
    try {
      const d = await (await fetch('/api/upload_photo', {method:'POST', body:fd})).json();
      if (d.path && !importedPhotos.includes(d.path)) importedPhotos.push(d.path);
    } catch(_) {}
  }
  document.getElementById('photo-file').value = '';
  renderPhotoStrip();
  saveLastSettings();
}

function renderVideoList() {
  const container = document.getElementById('video-list');
  container.innerHTML = '';
  videoList.forEach((v, i) => {
    const row = document.createElement('div');
    row.className = 'video-item';

    const top = document.createElement('div');
    top.className = 'vtop';

    const idx = document.createElement('span');
    idx.className = 'vidx';
    idx.textContent = i + 1;

    const name = document.createElement('span');
    name.className = 'vname';
    name.textContent = v.path;
    name.title = v.path;

    const pv = document.createElement('button');
    pv.className = 'btn-preview';
    pv.textContent = '▶';
    pv.title = '預覽影片';
    pv.onclick = () => previewVideo(v.path);

    const btn = document.createElement('button');
    btn.className = 'btn-remove';
    btn.textContent = '✕';
    btn.onclick = () => removeVideoFromList(i);

    top.appendChild(idx);
    top.appendChild(name);
    top.appendChild(pv);
    top.appendChild(btn);
    row.appendChild(top);

    const sub = document.createElement('div');
    sub.className = 'vanalysis';
    if (v.analyzing) {
      sub.innerHTML = '<span class="ana-loading">🔄 智慧分析中…</span>';
    } else if (v.analysis && v.analysis.error) {
      sub.innerHTML = '<span class="ana-err">⚠ 分析失敗：' + escapeHtml(v.analysis.error) + '</span>';
    } else if (v.analysis) {
      const cat = v.analysis.category || '其他';
      const catClass = cat === '車禍或槍戰' ? '車禍' : cat;
      const durTxt = v.analysis.duration != null
        ? '<span class="ana-dur">⏱ ' + v.analysis.duration + ' 秒</span> '
        : '';
      sub.innerHTML =
        '<span class="cat-badge cat-' + catClass + '">' + escapeHtml(cat) + '</span> ' +
        durTxt +
        '<span class="ana-desc-inline">' + escapeHtml(v.analysis.description || '') + '</span>';
    }
    if (sub.innerHTML) row.appendChild(sub);

    container.appendChild(row);
  });
  // 影片清單變動可能切換圖輯式模式提示
  const hint = document.getElementById('photo-mode-hint');
  if (hint) hint.style.display = (!videoList.length && importedPhotos.length) ? 'block' : 'none';
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// 依序（後端一次只能跑一個分析工作）對清單裡的影片跑智慧分析
async function analyzeQueued(items) {
  for (const item of items) {
    item.analyzing = true;
    renderVideoList();
    try {
      const r = await fetch('/api/analyze', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({video: item.path})
      });
      const d = await r.json();
      if (d.error) {
        item.analyzing = false;
        item.analysis = {error: d.error};
        renderVideoList();
        saveLastSettings();
        continue;
      }
      await new Promise(resolve => {
        const timer = setInterval(async () => {
          try {
            const sd = await (await fetch('/api/analyze_status')).json();
            if (sd.done) {
              clearInterval(timer);
              item.analyzing = false;
              item.analysis = sd.error ? {error: sd.error} : sd.result;
              renderVideoList();
              saveLastSettings();
              resolve();
            }
          } catch(_) {}
        }, 1200);
      });
    } catch(e) {
      item.analyzing = false;
      item.analysis = {error: e.message};
      renderVideoList();
      saveLastSettings();
    }
  }
}

// ─── 記住上次設定 ──────────────────────────────────────────────────────────
function saveLastSettings() {
  // 只存「設定類」偏好；素材（影片清單/照片）刻意不存——
  // 使用者指定：重新整理＝乾淨的開始，舊素材通通清掉（2026-07-10）
  localStorage.setItem('videoai_last_browse_dir', localStorage.getItem('videoai_last_browse_dir') || '');
  // 鍵名 v3：確認站升級為三態（smart/always/off），舊布林偏好作廢
  localStorage.setItem('videoai_ckmode', document.getElementById('checkpoint_mode').value);
  localStorage.setItem('videoai_lowq', document.getElementById('lowq').checked ? '1' : '0');
}

// 停站模式說明文字（依選擇即時更新）
const CKMODE_HINT = {
  smart:  '平常自動出片不打擾；只有事實查核有硬矛盾、譯名不符、素材不足或有黑幕段時才停下讓你看。',
  always: '每支都會停腳本站（看稿改稿）＋分鏡站（換畫面、快速預覽），逐支把關。',
  off:    '完全不停，一路跑完直接進成片庫。適合批次/過夜大量產製。',
};
function onCkModeChange() {
  const v = document.getElementById('checkpoint_mode').value;
  const h = document.getElementById('ckmode-hint');
  if (h) h.textContent = CKMODE_HINT[v] || '';
  saveLastSettings();
}

// POC 低畫質開關：勾選時（正式渲染就是快的低畫質）隱藏「快速預覽」按鈕，
// 取消勾選（上線完整畫質、正式渲染變慢）時預覽按鈕自動回來
function onLowqChange() {
  saveLastSettings();
  const b = document.getElementById('draft-btn');
  if (b) b.style.display = document.getElementById('lowq').checked ? 'none' : '';
}

function restoreLastSettings() {
  // 清掉舊版存過的素材/設定鍵（現制：素材不跨重新整理保留）
  ['videoai_voice', 'videoai_video_list', 'videoai_photos', 'videoai_checkpoint']
    .forEach(k => localStorage.removeItem(k));
  videoList = [];
  importedPhotos = [];
  const ckPref = localStorage.getItem('videoai_ckmode');
  if (ckPref && ['smart','always','off'].includes(ckPref))
    document.getElementById('checkpoint_mode').value = ckPref;
  onCkModeChange();   // 帶出對應說明文字
  // POC 低畫質為鎖定狀態：強制啟用、忽略任何舊偏好；上線解鎖時改這裡
  document.getElementById('lowq').checked = true;
  onLowqChange();   // 隱藏快速預覽按鈕（POC 直接渲染就是低畫質）
  renderVideoList();
  renderPhotoStrip();
}

restoreLastSettings();
resumeIfRunning();

// ─── 後臺影音清單（LTN 內部 API 匯入）───────────────────────────────────────
let backlogItems = [];

function _pad2(n) { return String(n).padStart(2, '0'); }

function _dateToParts(dateStr, isEnd) {
  // 輸入是 <input type=date> 的 "YYYY-MM-DD"，API 要 year + MMDDHHmm；
  // 起始日補 0000（當天開始），結束日補 2359（當天結束），涵蓋整天
  const [y, m, d] = dateStr.split('-');
  const hhmm = isEnd ? '2359' : '0000';
  return { year: y, mmddhhmm: m + d + hhmm };
}

function _initBacklogDefaults() {
  const now = new Date();
  const today = `${now.getFullYear()}-${_pad2(now.getMonth()+1)}-${_pad2(now.getDate())}`;
  document.getElementById('bl-start').value = today;
  document.getElementById('bl-end').value = today;
}
_initBacklogDefaults();

async function loadBacklog() {
  const startVal = document.getElementById('bl-start').value;
  const endVal = document.getElementById('bl-end').value;
  if (!startVal || !endVal) { alert('請選擇起始與結束日期'); return; }
  const s = _dateToParts(startVal, false);
  const e = _dateToParts(endVal, true);
  const list = document.getElementById('backlog-list');
  list.innerHTML = '<div style="padding:14px;color:#9ca3af;font-size:.82rem">載入中…</div>';
  try {
    const r = await fetch(`/api/backlog?year=${s.year}&start=${s.mmddhhmm}&end=${e.mmddhhmm}`);
    const d = await r.json();
    if (d.error) { list.innerHTML = `<div style="padding:14px;color:#b91c1c;font-size:.82rem">${d.error}</div>`; return; }
    backlogItems = d.items || [];
    renderBacklog();
  } catch (err) {
    list.innerHTML = `<div style="padding:14px;color:#b91c1c;font-size:.82rem">${err.message}</div>`;
  }
}

function renderBacklog(skipAssess) {
  const filter = document.getElementById('bl-status-filter').value;
  const list = document.getElementById('backlog-list');
  const rows = backlogItems.filter(it => !filter || it.status === filter);
  if (!rows.length) {
    list.innerHTML = '<div style="padding:14px;color:#9ca3af;font-size:.82rem">沒有符合篩選條件的項目</div>';
    return;
  }
  list.innerHTML = rows.map(it => {
    const ts = (it.articleCreateTime || '').replace(/^(\\d{4})(\\d{2})(\\d{2})(\\d{2})(\\d{2}).*/, '$2/$3 $4:$5');
    const vc = it.videoCount || (it.videos || []).length;
    const status = it.status || '未知';
    const a = it._assess;
    const assessHtml = a
      ? `<span class="bl-assess bl-assess-${a.level}" title="影片總長 ${a.total_sec}s">${a.label}</span>`
      : `<span class="bl-assess bl-assess-wait" id="bl-assess-${it.articleNo}">⋯評估中</span>`;
    const materialShort = a && a.level === 'red';
    return `<div class="backlog-row${a && a.level === 'green' ? ' backlog-row-ready' : ''}">
      <span class="bl-status bl-status-${status}">${status}</span>
      ${assessHtml}
      <span class="bl-title">${_escBl(it.title)}</span>
      <span class="bl-meta">${ts}・🎬${vc}支${a && a.total_sec ? '(' + a.total_sec + 's)' : ''}${a && a.n_photos != null ? '・📷' + a.n_photos + '張' : ''}</span>
      <button class="btn-import"${materialShort ? ' disabled title="素材不足，暫不建議匯入"' : ''} onclick="importBacklogItem('${it.articleNo}')" id="bl-import-${it.articleNo}">📥 匯入</button>
    </div>`;
  }).join('');
  if (!skipAssess) assessBacklogItems();
}

// 逐篇探測影片總長 → 標註「可一鍵生成／素材偏少／素材不足」（一次最多 3 篇並行）
let _assessRunning = false;
async function assessBacklogItems() {
  if (_assessRunning) return;
  _assessRunning = true;
  try {
    const queue = backlogItems.filter(it => !it._assess);
    const runOne = async () => {
      while (queue.length) {
        const it = queue.shift();
        try {
          const r = await fetch('/api/backlog/assess', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({videoUrls: (it.videos || []).map(v => v.url),
                                  articleNo: it.articleNo}),
          });
          const d = await r.json();
          // 這篇一評估完就立刻重繪，不等其他篇——避免一篇卡住讓全部列陪著空等
          if (!d.error) { it._assess = d; renderBacklog(true); }
        } catch (_) {}
      }
    };
    await Promise.all([runOne(), runOne(), runOne()]);
  } finally {
    _assessRunning = false;
  }
}

function _escBl(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function importBacklogItem(articleNo) {
  const it = backlogItems.find(x => x.articleNo === articleNo);
  if (!it) return;
  // 守門：素材不足（紅燈）一律擋掉，不管按鈕是否被點到（防快取/時序造成的誤點）
  if (it._assess && it._assess.level === 'red') {
    toast('素材不足，暫不建議匯入');
    return;
  }
  const btn = document.getElementById('bl-import-' + articleNo);
  btn.disabled = true;
  btn.textContent = '匯入中…';
  try {
    const videoUrls = (it.videos || []).map(v => v.url);
    const r = await fetch('/api/backlog/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({articleNo, videoUrls}),
    });
    const d = await r.json();
    if (d.error) { alert('匯入失敗：' + d.error); btn.disabled = false; btn.textContent = '📥 匯入'; return; }

    document.getElementById('article').value = d.content || '';
    // 匯入完成直接帶使用者到「輸入資料」的新聞稿區（不用自己往下捲）
    document.getElementById('article').scrollIntoView({behavior: 'smooth', block: 'start'});
    importedPhotos = d.photos || [];   // 換新聞就換配圖，不累積上一篇的
    // 關鍵字＝標籤條、配圖說明＝歸位到照片素材區（先前全擠在同一行難讀）
    const kwBox = document.getElementById('import-keywords');
    if (d.keywords && d.keywords.length) {
      kwBox.innerHTML = '🔑 文章關鍵字　' + d.keywords.map(k =>
        `<span style="display:inline-block;background:#e0e7ff;color:#3730a3;border-radius:6px;padding:1px 9px;margin-right:6px;font-size:.78rem">${escapeHtml(k)}</span>`).join('');
      kwBox.style.display = 'block';
    } else {
      kwBox.style.display = 'none';
    }
    const photoNote = document.getElementById('photo-import-note');
    if (importedPhotos.length) {
      photoNote.textContent = `已自動附上本篇 ${importedPhotos.length} 張新聞配圖（點縮圖可放大檢視）`;
      photoNote.style.display = 'block';
    } else {
      photoNote.style.display = 'none';
    }
    if (d.errors && d.errors.length) {
      alert('部分影片下載失敗：\\n' + d.errors.join('\\n'));
    }

    // 換新聞＝換素材：上一則還沒產製就匯入下一則時，影片清單整組換新不累積
    // （照片在上面已同樣處理），下載好的影片自動跑智慧分析
    videoList = [];
    const newItems = [];
    (d.downloaded || []).forEach(path => {
      if (!videoList.some(v => v.path === path)) {
        const item = {path, start: 0, analyzing: false, analysis: null};
        videoList.push(item);
        newItems.push(item);
      }
    });
    renderVideoList();
    renderPhotoStrip();
    saveLastSettings();
    if (newItems.length) analyzeQueued(newItems);
  } catch (e) {
    alert('匯入失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '📥 匯入';
  }
}

// ─── 資料夾瀏覽 modal ──────────────────────────────────────────────────────
let browseSelected = new Set();  // 已勾選的檔案路徑（跨資料夾保留）

function openBrowse() {
  const startPath = localStorage.getItem('videoai_last_browse_dir') || '';
  browseSelected.clear();
  updateAddSelBtn();
  document.getElementById('browse-modal').classList.add('show');
  loadBrowseDir(startPath);
}

function closeBrowse() {
  document.getElementById('browse-modal').classList.remove('show');
}

function updateAddSelBtn() {
  const btn = document.getElementById('btn-add-sel');
  btn.textContent = `加入選取（${browseSelected.size}）`;
  btn.disabled = browseSelected.size === 0;
}

function addSelectedToList() {
  const currentDir = document.getElementById('browse-path').value.trim();
  if (currentDir) localStorage.setItem('videoai_last_browse_dir', currentDir);

  const newItems = [];
  browseSelected.forEach(path => {
    if (!videoList.some(v => v.path === path)) {
      const item = {path, start: 0, analyzing: false, analysis: null};
      videoList.push(item);
      newItems.push(item);
    }
  });
  browseSelected.clear();
  renderVideoList();
  saveLastSettings();
  closeBrowse();

  if (newItems.length) analyzeQueued(newItems);
}

function browseGo() {
  loadBrowseDir(document.getElementById('browse-path').value.trim());
}

async function loadBrowseDir(path) {
  const list = document.getElementById('browse-list');
  list.innerHTML = '';
  list.appendChild(browseMsg('載入中…'));
  try {
    const r = await fetch('/api/browse?path=' + encodeURIComponent(path));
    const d = await r.json();
    if (d.error) {
      list.innerHTML = '';
      list.appendChild(browseMsg(d.error));
      return;
    }
    document.getElementById('browse-path').value = d.path;
    renderBrowseList(d);
  } catch (e) {
    list.innerHTML = '';
    list.appendChild(browseMsg(e.message));
  }
}

let browseFilePaths = [];   // 目前這個資料夾的檔案路徑（依畫面順序），供 Shift 範圍選取用
let browseLastIdx = -1;     // 上次點擊的檔案索引

function renderBrowseList(d) {
  const list = document.getElementById('browse-list');
  list.innerHTML = '';
  browseFilePaths = d.files.map(f => f.path);
  browseLastIdx = -1;
  if (d.parent) list.appendChild(browseItem('&#128193;', '.. 上一層', d.parent, true, -1));
  d.dirs.forEach(item => list.appendChild(browseItem('&#128193;', item.name, item.path, true, -1)));
  d.files.forEach((item, i) => list.appendChild(browseItem('&#127916;', item.name, item.path, false, i)));
  if (!d.dirs.length && !d.files.length) {
    list.appendChild(browseMsg('（空資料夾，沒有找到影片檔）'));
  }
}

function browseItem(iconHtml, label, path, isDir, fileIdx) {
  const div = document.createElement('div');
  div.className = 'modal-item';
  div.dataset.path = path;
  const ic = document.createElement('span');
  ic.className = 'ic';
  ic.innerHTML = iconHtml;
  div.appendChild(ic);
  div.appendChild(document.createTextNode(label));

  if (isDir) {
    div.onclick = () => loadBrowseDir(path);
  } else {
    // 點列（打勾）= 多選；按住 Shift 點另一列 = 範圍全選
    const chk = document.createElement('span');
    chk.className = 'chk';
    chk.textContent = '✓';
    div.appendChild(chk);
    if (browseSelected.has(path)) div.classList.add('selected');
    div.onclick = (e) => {
      if (e.shiftKey && browseLastIdx >= 0 && fileIdx >= 0) {
        // Shift：把上次點的到這次點的整個範圍設成「選取」
        const lo = Math.min(browseLastIdx, fileIdx);
        const hi = Math.max(browseLastIdx, fileIdx);
        for (let i = lo; i <= hi; i++) browseSelected.add(browseFilePaths[i]);
        syncBrowseSelectedClasses();
      } else {
        // 一般點擊：切換這一列
        if (browseSelected.has(path)) {
          browseSelected.delete(path);
          div.classList.remove('selected');
        } else {
          browseSelected.add(path);
          div.classList.add('selected');
        }
      }
      browseLastIdx = fileIdx;
      updateAddSelBtn();
    };
  }
  return div;
}

// 依 browseSelected 重新套用每一列的 selected 樣式（Shift 範圍選取後同步畫面）
function syncBrowseSelectedClasses() {
  document.querySelectorAll('#browse-list .modal-item').forEach(el => {
    if (el.dataset.path && browseSelected.has(el.dataset.path)) {
      el.classList.add('selected');
    }
  });
}

function browseMsg(text) {
  const div = document.createElement('div');
  div.className = 'modal-empty';
  div.textContent = text;
  return div;
}

// ─── 照片放大檢視（照片素材列點圖開燈箱，← → 鍵或畫面箭頭切換）──────────────
let lightboxIdx = 0;
function showPhotoLarge(i) {
  lightboxIdx = i;
  const path = importedPhotos[i];
  document.getElementById('photo-lightbox-img').src =
    '/api/photo?path=' + encodeURIComponent(path);
  document.getElementById('photo-lightbox-count').textContent =
    (importedPhotos.length > 1) ? (i + 1) + ' / ' + importedPhotos.length : '';
  document.getElementById('photo-lightbox').style.display = 'flex';
  // 圖說：先收起，再抓（記者圖說優先，其次已快取的 AI 辨識；快速切圖時只認最後一張）
  const capBox = document.getElementById('photo-lightbox-caption');
  capBox.style.display = 'none';
  fetch('/api/photo_caption?path=' + encodeURIComponent(path))
    .then(r => r.json())
    .then(d => {
      if (lightboxIdx !== i) return;          // 已切到別張，這次結果作廢
      if (!d.caption) return;                 // 無圖說就維持收起
      document.getElementById('photo-lightbox-caption-text').textContent = d.caption;
      const srcEl = document.getElementById('photo-lightbox-caption-src');
      srcEl.textContent = d.src || '';
      srcEl.style.background = (d.src === '記者圖說') ? '#2563eb' : '#6b7280';
      capBox.style.display = 'block';
    })
    .catch(() => {});
}
function stepPhoto(dir) {
  if (!importedPhotos.length) return;
  showPhotoLarge((lightboxIdx + dir + importedPhotos.length) % importedPhotos.length);
}
document.addEventListener('keydown', e => {
  const box = document.getElementById('photo-lightbox');
  if (!box || box.style.display !== 'flex') return;
  if (e.key === 'ArrowLeft') stepPhoto(-1);
  else if (e.key === 'ArrowRight') stepPhoto(1);
  else if (e.key === 'Escape') box.style.display = 'none';
});

// ─── 素材影片預覽 ───────────────────────────────────────────────────────────
function previewVideo(path) {
  const m = document.getElementById('pv-modal');
  const v = document.getElementById('pv-video');
  v.src = '/api/src_video?path=' + encodeURIComponent(path);
  const cut = Math.max(path.lastIndexOf('/'), path.lastIndexOf(String.fromCharCode(92)));
  document.getElementById('pv-name').textContent = path.substring(cut + 1);
  m.style.display = 'flex';
  // 播放時聲音自動打開（由 ▶ 點擊觸發，有使用者手勢，允許帶聲音播放）
  v.muted = false;
  v.volume = 1;
  v.play().catch(() => {});
}
function closePreviewVideo(ev) {
  if (ev && ev.target.id === 'pv-video') return;
  const v = document.getElementById('pv-video');
  v.pause(); v.removeAttribute('src'); v.load();
  document.getElementById('pv-modal').style.display = 'none';
}

</script>

<div id="photo-lightbox" onclick="this.style.display='none'"
     style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:210;
            align-items:center;justify-content:center;cursor:zoom-out">
  <span style="position:absolute;top:14px;right:22px;font-size:26px;color:#fff">✕</span>
  <span id="photo-lightbox-count" style="position:absolute;top:18px;left:22px;font-size:15px;color:#d1d5db"></span>
  <button onclick="event.stopPropagation();stepPhoto(-1)"
     style="position:absolute;left:14px;top:50%;transform:translateY(-50%);font-size:30px;color:#fff;
            background:rgba(0,0,0,.45);border:0;border-radius:50%;width:52px;height:52px;cursor:pointer">‹</button>
  <button onclick="event.stopPropagation();stepPhoto(1)"
     style="position:absolute;right:14px;top:50%;transform:translateY(-50%);font-size:30px;color:#fff;
            background:rgba(0,0,0,.45);border:0;border-radius:50%;width:52px;height:52px;cursor:pointer">›</button>
  <div style="display:flex;flex-direction:column;align-items:center;gap:10px;max-width:88vw">
    <img id="photo-lightbox-img" style="max-width:88vw;max-height:82vh;border-radius:8px">
    <div id="photo-lightbox-caption" onclick="event.stopPropagation()"
         style="display:none;max-width:760px;padding:10px 16px;border-radius:8px;
                background:rgba(255,255,255,.10);color:#f3f4f6;font-size:16px;line-height:1.7;
                text-align:center;cursor:default">
      <span id="photo-lightbox-caption-src"
            style="display:inline-block;margin-right:8px;padding:1px 8px;border-radius:10px;
                   font-size:12px;vertical-align:middle;background:#2563eb;color:#fff"></span>
      <span id="photo-lightbox-caption-text"></span>
    </div>
  </div>
</div>

<div id="pv-modal" onclick="closePreviewVideo(event)"
     style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:200;
            align-items:center;justify-content:center;flex-direction:column">
  <span style="position:absolute;top:14px;right:22px;font-size:26px;color:#fff;cursor:pointer">✕</span>
  <video id="pv-video" controls playsinline
         style="max-width:92vw;max-height:80vh;border-radius:10px;background:#000"></video>
  <div id="pv-name" style="color:#d1d5db;font-size:.82rem;margin-top:10px;font-family:monospace"></div>
</div>

</body>
</html>"""


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/upload_photo', methods=['POST'])
def api_upload_photo():
    """上傳照片素材（圖輯式模式用），存 input/photos/，回傳本機路徑"""
    f = request.files.get('photo')
    if not f or not f.filename:
        return jsonify({'error': '沒有檔案'}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.webp'}:
        return jsonify({'error': '只接受 jpg / png / webp'}), 400
    photo_dir = BASE / 'input' / 'photos'
    photo_dir.mkdir(parents=True, exist_ok=True)
    dest = photo_dir / f"upload-{uuid.uuid4().hex[:8]}{ext}"
    f.save(str(dest))
    return jsonify({'ok': True, 'path': str(dest), 'name': f.filename})


@app.route('/api/generate', methods=['POST'])
def api_generate():
    global _busy, _job
    data = request.json or {}
    article = (data.get('article') or '').strip()
    if not article:
        return jsonify({'error': '新聞稿不能為空'}), 400

    # 支援 videos:[{path,start},...]（多支）；相容舊版單一 video+start 欄位
    raw_videos = data.get('videos')
    if not raw_videos:
        single = (data.get('video') or '').strip()
        raw_videos = [{'path': single, 'start': data.get('start') or 0}] if single else []

    videos: list[dict] = []
    for v in raw_videos:
        path = (v.get('path') or '').strip()
        if not path:
            continue
        err = validate_video_path(path)
        if err:
            return jsonify({'error': err}), 400
        videos.append({'path': path, 'start': float(v.get('start') or 0)})

    with _lock:
        if _busy:
            return jsonify({'error': '目前有工作進行中，請稍後再試'}), 429
        _busy = True
        _job = {'step': 0, 'msg': '準備中', 'done': False,
                'error': None, 'filename': None, 'title': None, 'warning': None,
                'plan': None,
                'started_at': time.time(), 'step_started_at': time.time(),
                'elapsed_sec': 0, 'step_elapsed_sec': 0}

    mode = data.get('checkpoint_mode')
    if mode not in ('smart', 'always', 'off'):
        # 舊版相容：只帶 checkpoint bool 時，True→'always'（維持舊語意）、False→'off'
        cp = data.get('checkpoint')
        mode = 'smart' if cp is None else ('always' if cp else 'off')
    lowq = data.get('low_quality')
    lowq = True if lowq is None else bool(lowq)   # POC 期間預設低畫質
    photos = [p for p in (data.get('photos') or []) if p and Path(p).exists()]
    t = threading.Thread(
        target=run_job,
        args=(article, videos, data.get('fname'), data.get('voice') or 'hsiaochen',
              mode, photos, lowq),
        daemon=True,
    )
    t.start()
    return jsonify({'ok': True})


@app.route('/api/status')
def api_status():
    d = dict(_job)
    d.pop('preview', None)   # 預覽 payload 較大，只走 /api/preview，狀態輪詢保持輕量
    if not d.get('done'):
        now = time.time()
        d['elapsed_sec'] = round(now - d.get('started_at', now), 1)
        d['step_elapsed_sec'] = round(now - d.get('step_started_at', now), 1)
    return jsonify(d)


@app.route('/api/script_preview')
def api_script_preview():
    """旁白稿確認 checkpoint 的預覽 payload（稿＋SOT 安排＋查核＋譯名＋數據卡）"""
    return jsonify(_script_preview)


@app.route('/api/confirm_script', methods=['POST'])
def api_confirm_script():
    """旁白稿確認：go（可附修改後的旁白）/ cancel"""
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    if action not in ('go', 'cancel'):
        return jsonify({'error': 'bad action'}), 400
    if not _job.get('awaiting_script'):
        return jsonify({'error': '目前沒有等待中的旁白稿確認'}), 409
    nar = (data.get('narration') or '').strip()
    _script_edit[0] = nar or None
    _script_dropcard[0] = bool(data.get('drop_card'))
    _confirm_action[0] = action
    _confirm_event.set()
    return jsonify({'ok': True})


@app.route('/api/draft', methods=['POST'])
def api_draft():
    """快速預覽：分鏡確認暫停期間，用目前分鏡（含未確認的換片段）渲染 480p 草稿"""
    if not _job.get('awaiting_confirm'):
        return jsonify({'error': '預覽只在分鏡確認時可用'}), 409
    if _draft['running']:
        return jsonify({'error': '預覽已在渲染中'}), 429
    data = request.get_json(silent=True) or {}
    _draft.update({'running': True, 'error': None})
    threading.Thread(target=_render_draft,
                     args=(data.get('plan_edits') or [],
                           (data.get('title') or '').strip(),
                           (data.get('hook') or '').strip(),
                           data.get('plan_removals') or []),
                     daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/draft_status')
def api_draft_status():
    return jsonify({'running': _draft['running'], 'error': _draft['error'],
                    'ready': bool(_draft['file'] and not _draft['running']
                                  and not _draft['error'])})


@app.route('/api/draft_video')
def api_draft_video():
    f = _draft.get('file')
    if not f or not Path(f).exists():
        return 'no draft', 404
    resp = send_file(f, mimetype='video/mp4', conditional=True)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/preview')
def api_preview():
    """分鏡確認暫停時，前端一次性抓取的預覽內容（旁白/Hook/配對縮圖/字幕）"""
    return jsonify(_preview or {})


@app.route('/api/confirm', methods=['POST'])
def api_confirm():
    """使用者在分鏡確認 checkpoint 的決定：go=繼續渲染、cancel=放棄"""
    data = request.json or {}
    action = 'cancel' if data.get('action') == 'cancel' else 'go'
    _confirm_edits[0] = data.get('plan_edits') or None
    _confirm_meta[0] = {'title': (data.get('title') or '').strip(),
                        'hook': (data.get('hook') or '').strip(),
                        'removals': data.get('plan_removals') or []}
    if not _job.get('awaiting_confirm'):
        return jsonify({'error': '目前沒有等待確認的工作'}), 409
    _confirm_action[0] = action
    _confirm_event.set()
    return jsonify({'ok': True, 'action': action})


@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    global _abusy, _analysis
    data = request.json or {}
    video = (data.get('video') or '').strip()
    if not video:
        return jsonify({'error': '請填入影片路徑'}), 400
    err = validate_video_path(video)
    if err:
        return jsonify({'error': err}), 400

    with _alock:
        if _abusy:
            return jsonify({'error': '分析中，請稍候再試'}), 429
        _abusy = True
        _analysis = {'done': False, 'error': None, 'layer': 1, 'msg': '初始化…', 'result': None}

    use_vision = data.get('use_vision', True)
    t = threading.Thread(target=run_analysis, args=(video, use_vision), daemon=True)
    t.start()
    return jsonify({'ok': True})


@app.route('/api/analyze_status')
def api_analyze_status():
    return jsonify(_analysis)


@app.route('/api/browse')
def api_browse():
    raw = (request.args.get('path') or '').strip()
    p = Path(raw) if raw else (BASE / 'input')

    if p.is_file():
        p = p.parent
    if not p.exists():
        p = BASE / 'input'
        p.mkdir(parents=True, exist_ok=True)
    if not p.is_dir():
        return jsonify({'error': f'不是有效的資料夾：{raw}'}), 400

    try:
        entries = list(p.iterdir())
    except PermissionError:
        return jsonify({'error': f'沒有權限讀取：{p}'}), 403
    except OSError as e:
        return jsonify({'error': str(e)}), 400

    dirs = sorted(
        ({'name': e.name, 'path': str(e)} for e in entries if e.is_dir()),
        key=lambda x: x['name'].lower()
    )
    files = sorted(
        ({'name': e.name, 'path': str(e)} for e in entries
         if e.is_file() and e.suffix.lower() in VIDEO_EXTS),
        key=lambda x: x['name'].lower()
    )
    parent = str(p.parent) if p.parent != p else None
    return jsonify({'path': str(p), 'parent': parent, 'dirs': dirs, 'files': files})


# ── 後臺影音清單（LTN 內部 API 匯入，供自動化測試用）──────────────────────────
@app.route('/api/backlog/assess', methods=['POST'])
def api_backlog_assess():
    """探測一篇文章的影片總長＋配圖張數，回傳素材預判（清單標註用）"""
    data = request.json or {}
    urls = [u for u in (data.get('videoUrls') or []) if u]
    try:
        result = (ltn_api.assess_materials(urls) if urls
                  else {'total_sec': 0, 'n_videos': 0, 'level': 'red', 'label': '✕ 素材不足'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    # 配圖張數（LTN 圖檔編號遞增，逐號 HEAD 探測；已發布文章配圖不會變，永久快取）
    article_no = (data.get('articleNo') or '').strip()
    if article_no:
        result['n_photos'] = ltn_api.get_photo_count(article_no)
    return jsonify(result)


@app.route('/api/backlog')
def api_backlog():
    year  = (request.args.get('year') or '').strip()
    start = (request.args.get('start') or '').strip()
    end   = (request.args.get('end') or '').strip()
    if not (year and start and end):
        return jsonify({'error': '請提供 year/start/end（start/end 格式 MMDDHHmm）'}), 400
    try:
        items = ltn_api.fetch_backlog(year, start, end)
    except Exception as e:
        return jsonify({'error': f'抓後臺清單失敗：{e}'}), 502
    return jsonify({'items': items})


@app.route('/api/backlog/import', methods=['POST'])
def api_backlog_import():
    """匯入：抓文章全文＋關鍵字、下載該篇所有影片到 input/"""
    data = request.json or {}
    article_no = (data.get('articleNo') or '').strip()
    video_urls = data.get('videoUrls') or []
    if not article_no:
        return jsonify({'error': '缺少 articleNo'}), 400

    try:
        article = ltn_api.fetch_article(article_no)
    except Exception as e:
        return jsonify({'error': f'抓文章內容失敗：{e}'}), 502

    downloaded = []
    errors = []
    for url in video_urls:
        try:
            downloaded.append(ltn_api.download_video(url))
        except Exception as e:
            errors.append(f'{url}：{e}')

    # 文章主圖一併下載：配不到動態畫面的旁白段，可用照片+Ken Burns 取代黑幕
    # （只用記者放的新聞主圖，不碰文章內嵌的網傳影片——那走 LiTV，刻意不處理）
    photo_paths = []
    if article.get('photo_url'):
        try:
            # LTN 圖檔編號遞增，API 只回第一張——探測抓出同篇全部配圖
            for pi, purl in enumerate(ltn_api.discover_photos(article['photo_url']), 1):
                photo_paths.append(ltn_api.download_photo(purl, article_no, pi))
            # 主圖有記者手寫圖說就存起來，配對時優先用（免 GPT 猜、又準）
            if article.get('photo_caption'):
                ltn_api.save_photo_caption(article_no, article['photo_caption'])
        except Exception as e:
            errors.append(f"配圖下載失敗：{e}")

    return jsonify({
        'ok': True,
        'content': article['content'],
        'keywords': article['keywords'],
        'source_url': article['source_url'],
        'downloaded': downloaded,
        'photos': photo_paths,
        'errors': errors,
    })


@app.route('/api/logs')
def api_logs():
    limit = int(request.args.get('limit', 500))
    return jsonify(_read_logs(limit))


@app.route('/logs')
def page_logs():
    return render_template_string(LOGS_HTML)


def _find_output(filename: str):
    """依檔名在 output/（含日期子資料夾）遞迴找成片，回傳 Path 或 None"""
    if '/' in filename or '\\' in filename or '..' in filename:
        return None
    p = OUTPUT / filename
    if p.exists():
        return p
    return next((q for q in OUTPUT.rglob(filename) if q.is_file()), None)


@app.route('/api/download/<filename>')
def api_download(filename):
    path = _find_output(filename)
    if not path:
        return 'File not found', 404
    return send_file(str(path), as_attachment=True, download_name=filename)


@app.route('/api/video/<filename>')
def api_video(filename):
    """inline 串流成片（給成片庫播放器用，不是下載）"""
    path = _find_output(filename)
    if not path:
        return 'File not found', 404
    return send_file(str(path), mimetype='video/mp4', conditional=True)


@app.route('/api/src_video')
def api_src_video():
    """預覽素材影片（素材清單的 ▶ 按鈕用；限 input/ 底下，防目錄穿越）"""
    raw = request.args.get('path', '')
    input_dir = (BASE / 'input').resolve()
    try:
        p = Path(raw).resolve()
        p.relative_to(input_dir)
    except Exception:
        return 'forbidden', 403
    if not p.is_file():
        return 'not found', 404
    return send_file(str(p), mimetype='video/mp4', conditional=True)


@app.route('/api/thumb/<filename>')
def api_thumb(filename):
    """成片首幀縮圖（成片庫封面用），抽一次存快取，之後直接回快取檔"""
    path = _find_output(filename)
    if not path:
        return 'not found', 404
    thumb_dir = TMP / "gallery_thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb = thumb_dir / (filename.rsplit('.', 1)[0] + '.jpg')
    if not thumb.exists():
        from produce import ff
        try:
            ff("-ss", "3.5", "-i", str(path), "-vframes", "1",
               "-vf", "scale=270:-1", "-q:v", "5", thumb)
        except Exception:
            return 'thumb failed', 500
    if not thumb.exists():
        return 'thumb failed', 500
    return send_file(str(thumb), mimetype='image/jpeg')


@app.route('/api/photo')
def api_photo():
    """顯示 input/photos/ 底下的照片縮圖（限定該目錄，防目錄穿越）"""
    raw = request.args.get('path', '')
    photos_dir = (BASE / 'input' / 'photos').resolve()
    try:
        rp = Path(raw).resolve()
        if photos_dir not in rp.parents or not rp.is_file():
            return 'not found', 404
    except Exception:
        return 'not found', 404
    return send_file(str(rp))


@app.route('/api/photo_caption')
def api_photo_caption():
    """
    照片圖說（燈箱下方顯示用）：記者手寫圖說（主圖，API A_Photo.Content）優先，
    其次撿已快取的 AI 視覺辨識描述——只讀快取、不觸發新的 GPT 呼叫（開圖不花錢）。
    """
    raw = request.args.get('path', '')
    cap = ltn_api.get_photo_caption(raw)
    if cap:
        return jsonify({'caption': cap, 'src': '記者圖說'})
    try:
        cached = _photodesc_cache_get('photodesc1', Path(raw))
        if cached and cached.get('desc'):
            return jsonify({'caption': cached['desc'], 'src': 'AI 辨識'})
    except Exception:
        pass
    return jsonify({'caption': '', 'src': ''})


@app.route('/api/gallery')
def api_gallery():
    """成片庫：jobs.jsonl 裡成功產出、且檔案還在的成片，附完整 metadata，新到舊"""
    seen = set()
    items = []
    for r in _read_logs(1000):
        if r.get('type') != 'produce' or r.get('error') or r.get('cancelled'):
            continue
        fn = r.get('output_file') or ''
        if not fn or fn in seen:
            continue
        path = _find_output(fn)
        if not path:
            continue
        seen.add(fn)
        pp = float(r.get('prompt_tokens', 0)) / 1e6 * 5 + float(r.get('completion_tokens', 0)) / 1e6 * 15
        items.append({
            'filename': fn,
            'title': r.get('title') or fn.rsplit('.', 1)[0],
            'timestamp': r.get('timestamp', ''),
            'main_sec': r.get('main_sec', 0),
            'hook': r.get('hook', ''),
            'hashtags': r.get('hashtags', []),
            'narration': r.get('narration', ''),
            'article': r.get('article', ''),
            'plan': r.get('plan', []),
            'video_path': r.get('video_path', ''),
            'sot': r.get('sot'),
            'factcheck': r.get('factcheck') or [],
            'cost': round(pp, 4),
            'day': path.parent.name if path.parent != OUTPUT else '',
        })
    return jsonify(items)


@app.route('/api/gallery/delete', methods=['POST'])
def api_gallery_delete():
    fn = (request.json or {}).get('filename', '')
    path = _find_output(fn)
    if not path:
        return jsonify({'error': '找不到檔案'}), 404
    try:
        path.unlink()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rerun', methods=['POST'])
def api_rerun():
    """用某支成片/失敗 job 的原始輸入重跑（分析有快取，等於只重做後面的步驟）"""
    global _busy, _job
    data = request.json or {}
    fn = data.get('filename', '')
    jid = data.get('id', '')
    src = None
    for r in _read_logs(1000):
        if r.get('type') != 'produce':
            continue
        if (jid and r.get('id') == jid) or (fn and r.get('output_file') == fn):
            src = r
            break
    if not src:
        return jsonify({'error': '找不到對應的產製記錄'}), 404

    article = src.get('article', '')
    vpaths = [p.strip() for p in (src.get('video_path') or '').split(';') if p.strip()]
    videos = [{'path': p, 'start': 0} for p in vpaths if Path(p).exists()]
    if not article:
        return jsonify({'error': '原始記錄沒有新聞稿內容，無法重跑'}), 400

    with _lock:
        if _busy:
            return jsonify({'error': '目前有工作進行中，請稍後再試'}), 429
        _busy = True
        _job = {'step': 0, 'msg': '準備中（重跑）', 'done': False,
                'error': None, 'filename': None, 'title': None, 'warning': None,
                'plan': None, 'started_at': time.time(), 'step_started_at': time.time(),
                'elapsed_sec': 0, 'step_elapsed_sec': 0}
    photos = [p for p in (src.get('photos') or []) if Path(p).exists()]
    t = threading.Thread(target=run_job,
                         args=(article, videos, src.get('output_file', '').rsplit('.', 1)[0],
                               'hsiaochen', True, photos),
                         daemon=True)
    t.start()
    return jsonify({'ok': True, 'videos_found': len(videos), 'videos_expected': len(vpaths)})


@app.route('/gallery')
def page_gallery():
    return render_template_string(GALLERY_HTML)


if __name__ == '__main__':
    print("[VideoAI] Starting on http://localhost:5000")
    # threaded=True：後臺清單/文章/影片下載這幾個新端點是同步處理，
    # 沒有這個參數整台伺服器只能一次處理一個請求，下載影片時會卡住其他所有請求
    # （含正在跑的產製工作的 /api/status 輪詢）
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
