"""
社會新聞短影音產製 Web UI
用法：
  cd D:\\VideoAI\\scripts && python app.py
  瀏覽器開 http://localhost:5000
"""

import datetime
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

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
    generate_script, shorten_script, generate_tts, write_ass,
    align_subtitles_to_boundaries, rescale_subtitles, rescale_segments,
    _probe_duration,
    make_intro, make_main, make_main_plan, make_outro, concat,
)
from select_clip import select_clip, catalog_video, match_narration_to_clips

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
         '旁白配對畫面',
         '組裝片頭', '組裝主畫面', '組裝片尾', '串接輸出']


def run_job(article: str, videos: list[dict], fname: str | None, voice_key: str = "hsiaochen"):
    """videos: [{"path": "...", "start": 12.0}, ...]，依序銜接補滿 MAIN_SEC 秒"""
    global _busy, _job
    voice = TTS_VOICES.get(voice_key, TTS_VOICES["hsiaochen"])

    t0 = time.time()
    job_id = str(uuid.uuid4())[:8]
    model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    token_usage: dict = {}
    output_name: str | None = None
    err_msg: str | None = None
    length_info: dict = {}
    step_durations: dict = {}
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

        tts_path = TMP / "_narration.mp3"
        ass_path = TMP / "_subtitles.ass"

        # ── 步驟1：先分析素材（write to picture 的前提：先知道有什麼畫面）────────
        p(1)
        catalogs = []
        if videos:
            for i, v in enumerate(videos):
                _job['msg'] = f"{STEPS[1]}（{i+1}/{len(videos)}）"
                try:
                    cat = catalog_video(Path(v['path']))
                    _add_tokens(cat.get('tokens', {}))
                    catalogs.append(cat)
                except Exception:
                    pass  # 單支分析失敗不擋整體流程

        # 素材畫面清單 → 給 GPT 貼著畫面寫稿
        footage_notes = None
        if catalogs:
            lines = []
            for vi, cat in enumerate(catalogs):
                lines.append(f"影片V{vi+1}（{Path(cat['path']).name}，總長 {cat['duration']} 秒）：")
                for seg in cat['segments']:
                    lines.append(f"  {seg['start']}~{seg['end']}秒：{seg.get('description', '')}")
            footage_notes = "\n".join(lines)

        p(2)
        script, script_tokens = generate_script(article, footage_notes)
        _add_tokens(script_tokens)
        _job['title'] = script['title']

        # 長度保險 A：GPT 沒守字數上限就退件縮寫（多一次便宜的 GPT 呼叫）
        max_chars = int(MAIN_SEC * 4.6)
        if len(script['narration']) > max_chars:
            _job['msg'] = f"{STEPS[2]}（旁白 {len(script['narration'])} 字超限，縮寫中）"
            try:
                script, sh_tokens = shorten_script(script, int(MAIN_SEC * 4.4))
                _add_tokens(sh_tokens)
                _job['title'] = script['title']
            except Exception:
                pass  # 縮寫失敗就用原稿，交給長度保險 B

        p(3)
        boundaries = generate_tts(script['narration'], tts_path, voice=voice)
        tts_sec = _probe_duration(tts_path)

        # 長度保險 B：唸完還是太長 → 按比例加快語速重唸一次（上限 +20%，太快會不自然）
        if tts_sec > MAIN_SEC + 4:
            base = 1.08  # 目前預設 +8%
            need = base * tts_sec / (MAIN_SEC + 1)
            pct = min(int(round((need - 1) * 100)), 20)
            _job['msg'] = f"{STEPS[3]}（旁白 {tts_sec:.0f}s 過長，以 +{pct}% 語速重唸）"
            boundaries = generate_tts(script['narration'], tts_path, voice=voice, rate=f"+{pct}%")
            tts_sec = _probe_duration(tts_path)

        # 量測旁白實際長度，主畫面/字幕/配對全部對齊它（不再假設剛好 MAIN_SEC）
        main_sec = round(min(max(tts_sec + 0.4, 15.0), 90.0), 1)
        _job['main_sec'] = main_sec

        p(4)
        # 優先用 TTS 逐字時間精確對齊字幕；拿不到（或字數差太多）才退回等比縮放
        subtitles = align_subtitles_to_boundaries(script['subtitles'], boundaries) if boundaries else []
        _job['sub_align'] = '逐字對齊' if subtitles else '等比縮放(fallback)'
        if not subtitles:
            subtitles = rescale_subtitles(script['subtitles'], float(MAIN_SEC), main_sec)
        segments = rescale_segments(script.get('segments', []), float(MAIN_SEC), main_sec)
        write_ass(subtitles, ass_path)

        # ── 步驟5：旁白配畫面（腳本已貼素材寫，配對命中率高很多）──────────────────
        plan = None
        if catalogs and segments:
            p(5)
            try:
                plan, match_tokens = match_narration_to_clips(
                    segments, catalogs, main_sec)
                _add_tokens(match_tokens)
                _job['plan'] = [
                    {'video': (Path(e['path']).name if e.get('path') else '（黑幕）'),
                     'start': e.get('start', 0), 'dur': e['dur'],
                     'why': e.get('why', '')}
                    for e in plan
                ]
            except Exception:
                plan = None  # 配對失敗退回依序銜接

        p(6)
        intro = make_intro(script)

        p(7)
        if plan:
            main_clip, length_info = make_main_plan(tts_path, ass_path, plan, main_sec)
        else:
            main_clip, length_info = make_main(tts_path, ass_path, videos, main_sec)
        if length_info.get("insufficient"):
            _job['warning'] = (
                f"來源畫面只涵蓋 {length_info['covered_sec']} 秒，"
                f"不足 {length_info['target_sec']} 秒，"
                f"其餘 {length_info['shortfall_sec']} 秒為黑幕空景"
            )

        p(8)
        outro = make_outro()

        p(9)
        safe = re.sub(r'[\\/:*?"<>|]', '-', script['title'])[:20]
        name = (fname.strip() if fname else safe) or safe
        if not name.endswith('.mp4'):
            name += '.mp4'
        out_path = OUTPUT / name
        concat([intro, main_clip, outro], out_path)
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
            "main_sec":           _job.get('main_sec', 0),
            "step_durations":     step_durations,
            "error":              err_msg,
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
LOGS_HTML = """<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>使用記錄 — VideoAI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Microsoft JhengHei",sans-serif;background:#f3f4f6;color:#111;padding:24px}
h1{font-size:1.15rem;font-weight:700;margin-bottom:18px}
.back{font-size:.82rem;color:#6b7280;text-decoration:none;margin-right:14px}
.back:hover{color:#111}
.stat-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:#fff;border-radius:8px;padding:14px 18px;flex:1;min-width:130px;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.stat .val{font-size:1.4rem;font-weight:700;color:#2563eb}
.stat .lbl{font-size:.75rem;color:#888;margin-top:2px}
.price-row{display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:.83rem;color:#555}
.price-row input{width:90px;padding:5px 8px;border:1px solid #d1d5db;border-radius:5px;font-size:.83rem}
.tbl-wrap{overflow-x:auto;background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.07)}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{background:#f9fafb;padding:10px 12px;text-align:left;font-weight:600;color:#555;border-bottom:2px solid #e5e7eb;white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top;color:#374151}
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
</style>
</head>
<body>
<div style="display:flex;align-items:center;margin-bottom:18px">
  <a class="back" href="/">&larr; 返回產製</a>
  <h1>📋 使用記錄</h1>
</div>

<!-- 統計卡片 -->
<div class="stat-row" id="stats"></div>

<!-- 價格設定 -->
<div class="price-row">
  <span>估算費用單價（USD / 1M tokens）</span>
  <span>Prompt</span>
  <input type="number" id="p-price" value="5" step="0.5" min="0">
  <span>Completion</span>
  <input type="number" id="c-price" value="15" step="0.5" min="0">
  <span style="color:#aaa;font-size:.77rem">（Azure 實際費用請查帳單，此為 GPT-4o 級別參考值）</span>
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
  <thead>
    <tr>
      <th>時間</th>
      <th>類型</th>
      <th>模型</th>
      <th>時長(s)</th>
      <th>Prompt<br>tokens</th>
      <th>Completion<br>tokens</th>
      <th>影格數</th>
      <th>估算費用<br>(USD)</th>
      <th>稿件字數</th>
      <th>影片長度</th>
      <th>分類</th>
      <th>輸出檔</th>
      <th>狀態</th>
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
  const pp = parseFloat(document.getElementById('p-price').value) || 5;
  const cp = parseFloat(document.getElementById('c-price').value) || 15;
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
  const pp = parseFloat(document.getElementById('p-price').value) || 5;
  const cp = parseFloat(document.getElementById('c-price').value) || 15;

  let rows = _allLogs.filter(r => {
    if (fType && r.type !== fType) return false;
    if (fStatus === 'ok' && r.error) return false;
    if (fStatus === 'err' && !r.error) return false;
    if (kw && !JSON.stringify(r).toLowerCase().includes(kw)) return false;
    return true;
  });

  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map(r => {
    const cost = ((r.prompt_tokens||0)/1e6*pp + (r.completion_tokens||0)/1e6*cp);
    const ts = r.timestamp.replace('T',' ');
    const typeBadge = r.type === 'produce'
      ? '<span class="badge badge-produce">產製</span>'
      : '<span class="badge badge-analyze">分析</span>';
    const statusBadge = r.error
      ? `<span class="badge badge-err" title="${r.error}">失敗</span>`
      : '<span class="badge badge-ok">成功</span>';
    const na = '<span class="na">—</span>';
    return `<tr>
      <td class="mono">${ts}</td>
      <td>${typeBadge}</td>
      <td class="mono" style="font-size:.75rem">${r.model||na}</td>
      <td>${r.duration_sec||0}</td>
      <td>${fmtNum(r.prompt_tokens)}</td>
      <td>${fmtNum(r.completion_tokens)}</td>
      <td>${r.frames_sent||na}</td>
      <td class="mono">${cost > 0 ? '$'+cost.toFixed(5) : na}</td>
      <td>${r.article_chars||na}</td>
      <td>${videoLenCell(r)}</td>
      <td>${r.category||na}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.output_file||''}">${r.output_file||na}</td>
      <td>${statusBadge}</td>
    </tr>`;
  }).join('');

  renderStats(rows);
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
  const pp = parseFloat(document.getElementById('p-price').value) || 5;
  const cp = parseFloat(document.getElementById('c-price').value) || 15;
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
<title>短影音產製</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Microsoft JhengHei",sans-serif;background:#f3f4f6;color:#111;padding:28px 24px}
h1{font-size:1.25rem;font-weight:700;margin-bottom:22px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;max-width:980px}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
.card{background:#fff;border-radius:10px;padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{font-size:.9rem;font-weight:600;color:#666;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #f0f0f0}
label{display:block;font-size:.82rem;font-weight:500;color:#555;margin:14px 0 5px}
label:first-of-type{margin-top:0}
textarea,input[type=text],input[type=number]{
  width:100%;padding:9px 11px;border:1px solid #d1d5db;border-radius:6px;
  font-size:.88rem;font-family:inherit;background:#fafafa;color:#111;
  transition:border .15s
}
textarea:focus,input:focus{outline:none;border-color:#2563eb;background:#fff}
textarea{resize:vertical;min-height:210px}
.hint{font-size:.76rem;color:#aaa;margin-top:3px}
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
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:22px">
  <h1 style="margin:0">🎬 短影音產製</h1>
  <a href="/logs" style="font-size:.82rem;color:#6b7280;text-decoration:none;padding:6px 12px;border:1px solid #d1d5db;border-radius:6px">📋 使用記錄</a>
</div>
<div class="grid">

  <!-- 左：表單 -->
  <div class="card">
    <h2>輸入資料</h2>

    <label>新聞稿內容 <span style="color:#e00">*</span></label>
    <textarea id="article" placeholder="貼上新聞稿全文…"></textarea>

    <label>影片（可瀏覽選取多支，依序銜接補滿主畫面秒數）</label>
    <button class="btn-browse" style="width:100%;padding:11px" onclick="openBrowse()">&#128193; 瀏覽並選取影片</button>
    <p class="hint">選取後按「加入選取」，每支會自動跑智慧分析，結果顯示在檔名下方（分析過的有快取不重複收費）</p>

    <div id="video-list" class="video-list"></div>

    <label>旁白聲音</label>
    <select id="voice">
      <option value="hsiaochen">曉臻（女聲，預設）</option>
      <option value="hsiaoyu">曉雨（女聲）</option>
      <option value="yunjhe" disabled>雲哲（男聲，Microsoft 服務端暫時故障）</option>
    </select>

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
      <li id="s6"><div class="step-row"><span class="icon">○</span>組裝片頭（3 秒）<span class="step-time" id="t6"></span></div><div class="step-mini"><div class="step-mini-bar" id="m6"></div></div></li>
      <li id="s7"><div class="step-row"><span class="icon">○</span>組裝主畫面（長度隨旁白）<span class="step-time" id="t7"></span></div><div class="step-mini"><div class="step-mini-bar" id="m7"></div></div></li>
      <li id="s8"><div class="step-row"><span class="icon">○</span>組裝片尾（5 秒）<span class="step-time" id="t8"></span></div><div class="step-mini"><div class="step-mini-bar" id="m8"></div></div></li>
      <li id="s9"><div class="step-row"><span class="icon">○</span>串接輸出<span class="step-time" id="t9"></span></div><div class="step-mini"><div class="step-mini-bar" id="m9"></div></div></li>
    </ul>

    <div class="result" id="result">
      <p>✅ 產製完成！《<span id="titleText"></span>》</p>
      <video id="preview-video" controls style="width:100%;border-radius:8px;margin-bottom:10px;background:#000"></video>
      <div id="plan-box" style="display:none;margin-bottom:10px">
        <p style="font-size:.8rem;color:#374151;font-weight:600;margin-bottom:6px">🎞 旁白配對畫面明細</p>
        <div id="plan-rows" style="font-size:.76rem;color:#555;font-family:monospace;line-height:1.7"></div>
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
        voice:   document.getElementById('voice').value,
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
                     '組裝片頭', '組裝主畫面', '組裝片尾', '串接輸出'];
const N_STEPS = 9;

async function poll() {
  try {
    const d = await (await fetch('/api/status')).json();
    renderSteps(d);
    if (d.done) {
      clearInterval(timer);
      document.getElementById('btn').disabled = false;
      handleJobDone(d);
    }
  } catch(_) {}
}

function handleJobDone(d) {
  // 記下這支工作「已經看過結果了」，避免下次開網頁又跳出同一支的完成畫面
  if (d.started_at) localStorage.setItem('videoai_last_seen_job', String(d.started_at));
  if (d.error) {
    showErr(d.error);
  } else if (d.filename) {
    showResult(d.filename, d.title, d.warning, d.plan);
    // 產製成功 → 清空影片清單，準備下一支
    videoList = [];
    renderVideoList();
    localStorage.setItem('videoai_video_list', '[]');
  }
}

// 換分頁/視窗、或重新整理頁面後，自動接上仍在跑（或剛跑完還沒看過結果）的工作；
// 已經看過結果的舊工作不會重複跳出來
async function resumeIfRunning() {
  try {
    const d = await (await fetch('/api/status')).json();
    if (!d.done) {
      document.getElementById('btn').disabled = true;
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

function showResult(fname, title, warning, plan) {
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

function removeVideoFromList(index) {
  videoList.splice(index, 1);
  renderVideoList();
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

    const btn = document.createElement('button');
    btn.className = 'btn-remove';
    btn.textContent = '✕';
    btn.onclick = () => removeVideoFromList(i);

    top.appendChild(idx);
    top.appendChild(name);
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
  // 分析結果只存分類/描述（不含 error 物件裡可能的大型內容），避免 localStorage 爆量
  const slim = videoList.map(v => ({
    path: v.path, start: v.start,
    analysis: v.analysis ? {category: v.analysis.category, description: v.analysis.description,
                             duration: v.analysis.duration, error: v.analysis.error} : null
  }));
  localStorage.setItem('videoai_video_list', JSON.stringify(slim));
  localStorage.setItem('videoai_last_browse_dir', localStorage.getItem('videoai_last_browse_dir') || '');
  localStorage.setItem('videoai_voice', document.getElementById('voice').value);
}

function restoreLastSettings() {
  const voice = localStorage.getItem('videoai_voice');
  if (voice) document.getElementById('voice').value = voice;
  try {
    const saved = JSON.parse(localStorage.getItem('videoai_video_list') || '[]');
    if (Array.isArray(saved)) videoList = saved;
  } catch(_) { videoList = []; }
  renderVideoList();
}

restoreLastSettings();
resumeIfRunning();
document.getElementById('voice').addEventListener('change', saveLastSettings);

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


</script>

</body>
</html>"""


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)


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

    t = threading.Thread(
        target=run_job,
        args=(article, videos, data.get('fname'), data.get('voice') or 'hsiaochen'),
        daemon=True,
    )
    t.start()
    return jsonify({'ok': True})


@app.route('/api/status')
def api_status():
    d = dict(_job)
    if not d.get('done'):
        now = time.time()
        d['elapsed_sec'] = round(now - d.get('started_at', now), 1)
        d['step_elapsed_sec'] = round(now - d.get('step_started_at', now), 1)
    return jsonify(d)


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


@app.route('/api/logs')
def api_logs():
    limit = int(request.args.get('limit', 500))
    return jsonify(_read_logs(limit))


@app.route('/logs')
def page_logs():
    return render_template_string(LOGS_HTML)


@app.route('/api/download/<filename>')
def api_download(filename):
    path = OUTPUT / filename
    if not path.exists():
        return 'File not found', 404
    return send_file(str(path), as_attachment=True, download_name=filename)


if __name__ == '__main__':
    print("[VideoAI] Starting on http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
