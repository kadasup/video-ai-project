"""
數據動態字卡：把新聞裡的關鍵數字做成 2.8 秒的直式動畫插卡（1080x1920）。

做法借鏡中央社 HyperFrames：HTML＋資料化時間線（deterministic timeline）——
不即時錄影，而是逐幀 seek 動畫進度截圖再用 ffmpeg 組裝，幀幀精準、無掉幀。
瀏覽器用系統內建 Edge（playwright channel=msedge），不用另外下載 Chromium。

動畫設計：深色漸層底 → 標籤淡入 → 大數字 count-up（easeOut）→ 單位/補注淡入。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from select_clip import TMP, _ffmpeg_exe

FPS = 30
DUR = 2.8          # stat 卡秒數（向下相容；各類型秒數見 DUR_BY_TYPE）
W, H = 1080, 1920

# 資訊卡家族：stat 數據卡 / timeline 時序卡 / alert 警示卡 / chart 圖表卡
DUR_BY_TYPE = {"stat": 2.8, "timeline": 3.6, "alert": 2.5, "chart": 3.4}
TYPE_NAMES = {"stat": "數據卡", "timeline": "時序卡", "alert": "警示卡", "chart": "圖表卡"}


def card_duration(card: dict) -> float:
    return DUR_BY_TYPE.get((card or {}).get("type", "stat"), 2.8)

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:540px;height:960px;overflow:hidden;
  font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif;
  background:linear-gradient(160deg,#0b1526 0%,#12233f 55%,#0b1526 100%);
  display:flex;align-items:center;justify-content:center}
.card{text-align:center;width:100%;padding:0 40px}
.label{color:#7dd3fc;font-size:44px;font-weight:700;letter-spacing:6px;
  opacity:0;margin-bottom:28px}
.bigrow{display:flex;align-items:baseline;justify-content:center;gap:10px;flex-wrap:nowrap}
.num{color:#ffffff;font-size:150px;font-weight:900;line-height:1;white-space:nowrap;
  font-variant-numeric:tabular-nums;opacity:0;
  text-shadow:0 0 60px rgba(125,211,252,.35)}
.unit{color:#e2e8f0;font-size:52px;font-weight:700;opacity:0;white-space:nowrap}
.note{color:#94a3b8;font-size:34px;margin-top:34px;opacity:0}
.bar{width:0;height:6px;background:linear-gradient(90deg,#38bdf8,#818cf8);
  margin:26px auto 0;border-radius:3px}
</style></head><body>
<div class="card">
  <div class="label" id="label"></div>
  <div class="bigrow">
    <div class="num" id="num">0</div>
    <div class="unit" id="unit"></div>
  </div>
  <div class="bar" id="bar"></div>
  <div class="note" id="note"></div>
</div>
<script>
const D = %DATA%;
document.getElementById('label').textContent = D.label;
document.getElementById('unit').textContent  = D.unit;
document.getElementById('note').textContent  = D.note || '';
const target = D.target || 0;   // 數值與顯示格式由 Python 端解析（支援 1萬1400、6000萬、3.5）
// 長數字自動縮字級：用「數字唸完的最終寬度」實測，縮到整行放得下為止
(function(){
  const el = document.getElementById('num');
  const row = el.parentElement;
  el.textContent = fmtNum(target);   // 先放最終值量寬度
  let f = 150;
  el.style.fontSize = f + 'px';
  while (row.scrollWidth > row.clientWidth + 1 && f > 56) {
    f -= 6;
    el.style.fontSize = f + 'px';
  }
})();
function fmtNum(v) {
  if (D.mode === 'yi') {
    const y = Math.floor(v / 1e8), r = Math.round((v % 1e8) / 1e4);
    return y + '億' + (r ? r + '萬' : '');
  }
  if (D.mode === 'wan') {
    const w = Math.floor(v / 1e4), r = Math.round(v % 1e4);
    return w + '萬' + (r ? r.toLocaleString('en-US') : '');
  }
  return D.isInt ? Math.round(v).toLocaleString('en-US') : v.toFixed(1);
}

// 資料化時間線：p ∈ [0,1] 完全決定畫面狀態（可逐幀 seek，不靠即時播放）
const easeOut = t => 1 - Math.pow(1 - t, 3);
const seg = (p, a, b) => Math.min(1, Math.max(0, (p - a) / (b - a)));
function setProgress(p) {
  const lb = document.getElementById('label');
  lb.style.opacity = seg(p, 0.00, 0.15);
  lb.style.transform = `translateY(${(1 - easeOut(seg(p, 0, 0.18))) * 30}px)`;
  const k = easeOut(seg(p, 0.10, 0.62));
  document.getElementById('num').textContent = fmtNum(target * k);
  document.getElementById('num').style.opacity = seg(p, 0.08, 0.2);
  document.getElementById('unit').style.opacity = seg(p, 0.30, 0.45);
  document.getElementById('bar').style.width = (easeOut(seg(p, 0.15, 0.7)) * 320) + 'px';
  document.getElementById('note').style.opacity = seg(p, 0.5, 0.68);
}
setProgress(0);
</script></body></html>"""


_HTML_TIMELINE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:540px;height:960px;overflow:hidden;
  font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif;
  background:linear-gradient(160deg,#0b1526 0%,#12233f 55%,#0b1526 100%);
  display:flex;align-items:center;justify-content:center}
.card{width:100%;padding:0 52px}
.title{color:#7dd3fc;font-size:42px;font-weight:700;letter-spacing:5px;
  text-align:center;opacity:0;margin-bottom:44px}
.step{display:flex;align-items:flex-start;gap:20px;margin-bottom:34px;opacity:0}
.dotcol{display:flex;flex-direction:column;align-items:center;flex:none;padding-top:6px}
.dot{width:18px;height:18px;border-radius:50%;background:#ffd640;flex:none;
  box-shadow:0 0 16px rgba(255,214,64,.5)}
.line{width:4px;flex:1;min-height:34px;background:#2e8bff;margin-top:8px;border-radius:2px}
.stext{color:#fff;font-size:40px;font-weight:700;line-height:1.4}
</style></head><body>
<div class="card">
  <div class="title" id="title"></div>
  <div id="steps"></div>
</div>
<script>
const D = %DATA%;
document.getElementById('title').textContent = D.title || '';
const box = document.getElementById('steps');
D.steps.forEach((s, i) => {
  const div = document.createElement('div');
  div.className = 'step';
  div.innerHTML = '<div class="dotcol"><div class="dot"></div>'
    + (i < D.steps.length - 1 ? '<div class="line"></div>' : '') + '</div>'
    + '<div class="stext"></div>';
  div.querySelector('.stext').textContent = s;
  box.appendChild(div);
});
const easeOut = t => 1 - Math.pow(1 - t, 3);
const seg = (p, a, b) => Math.min(1, Math.max(0, (p - a) / (b - a)));
function setProgress(p) {
  const t = document.getElementById('title');
  t.style.opacity = seg(p, 0, 0.12);
  const n = D.steps.length;
  const slot = 0.72 / n;
  document.querySelectorAll('.step').forEach((el, i) => {
    const k = easeOut(seg(p, 0.12 + i * slot, 0.12 + i * slot + 0.16));
    el.style.opacity = k;
    el.style.transform = 'translateY(' + (1 - k) * 26 + 'px)';
  });
}
setProgress(0);
</script></body></html>"""

_HTML_ALERT = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:540px;height:960px;overflow:hidden;
  font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif;
  background:linear-gradient(160deg,#0b1526 0%,#12233f 55%,#0b1526 100%);
  display:flex;align-items:center;justify-content:center}
.card{text-align:center;width:100%;padding:0 40px}
.icon{font-size:190px;line-height:1.15;opacity:0}
.headline{color:#ffd640;font-size:82px;font-weight:900;margin-top:18px;opacity:0;
  white-space:nowrap;text-shadow:0 0 50px rgba(255,214,64,.3)}
.sub{color:#cbd5e1;font-size:42px;font-weight:700;margin-top:22px;opacity:0;white-space:nowrap}
.bar{width:0;height:6px;background:linear-gradient(90deg,#ffd640,#f59e0b);
  margin:30px auto 0;border-radius:3px}
</style></head><body>
<div class="card">
  <div class="icon" id="icon"></div>
  <div class="headline" id="headline"></div>
  <div class="sub" id="sub"></div>
  <div class="bar" id="bar"></div>
</div>
<script>
const D = %DATA%;
const ICONS = {'颱風':'🌀','豪雨':'🌧️','雷雨':'⛈️','地震':'⚠️','火災':'🔥',
               '警報':'⚠️','強風':'💨','大浪':'🌊','停電':'⚡','低溫':'🥶'};
document.getElementById('icon').textContent = ICONS[D.icon] || '⚠️';
document.getElementById('headline').textContent = D.headline || '';
document.getElementById('sub').textContent = D.sub || '';
// 文字過寬自動縮字級（單行不折行）
function fit(el, max, min) {
  let f = max;
  el.style.fontSize = f + 'px';
  while (el.scrollWidth > el.parentElement.clientWidth - 4 && f > min) {
    f -= 4;
    el.style.fontSize = f + 'px';
  }
}
fit(document.getElementById('headline'), 82, 50);
fit(document.getElementById('sub'), 42, 28);
const easeOut = t => 1 - Math.pow(1 - t, 3);
const seg = (p, a, b) => Math.min(1, Math.max(0, (p - a) / (b - a)));
function setProgress(p) {
  const ic = document.getElementById('icon');
  const k = easeOut(seg(p, 0, 0.3));
  ic.style.opacity = seg(p, 0, 0.18);
  ic.style.transform = 'scale(' + (0.55 + 0.45 * k) + ')';
  const hd = document.getElementById('headline');
  const k2 = easeOut(seg(p, 0.16, 0.38));
  hd.style.opacity = seg(p, 0.16, 0.32);
  hd.style.transform = 'translateY(' + (1 - k2) * 24 + 'px)';
  document.getElementById('sub').style.opacity = seg(p, 0.38, 0.55);
  document.getElementById('bar').style.width = (easeOut(seg(p, 0.2, 0.7)) * 330) + 'px';
}
setProgress(0);
</script></body></html>"""

_HTML_CHART = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:540px;height:960px;overflow:hidden;
  font-family:"Microsoft JhengHei","Noto Sans TC",sans-serif;
  background:linear-gradient(160deg,#0b1526 0%,#12233f 55%,#0b1526 100%);
  display:flex;align-items:center;justify-content:center}
.card{width:100%;padding:0 48px;text-align:center}
.title{color:#7dd3fc;font-size:42px;font-weight:700;letter-spacing:4px;opacity:0}
.unit{color:#64748b;font-size:28px;margin-top:6px;opacity:0}
.chart{display:flex;align-items:flex-end;justify-content:center;gap:22px;
  height:430px;margin-top:46px}
.col{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
  height:100%;flex:1;max-width:96px}
.val{color:#fff;font-size:34px;font-weight:900;margin-bottom:8px;opacity:0;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.b{width:100%;height:0;border-radius:8px 8px 0 0;
  background:linear-gradient(180deg,#38bdf8,#2456b3)}
.b.max{background:linear-gradient(180deg,#ffd640,#f59e0b)}
.xl{color:#94a3b8;font-size:30px;font-weight:700;margin-top:12px;white-space:nowrap}
</style></head><body>
<div class="card">
  <div class="title" id="title"></div>
  <div class="unit" id="unit"></div>
  <div class="chart" id="chart"></div>
</div>
<script>
const D = %DATA%;
document.getElementById('title').textContent = D.title || '';
document.getElementById('unit').textContent = D.unit ? '（' + D.unit + '）' : '';
const maxv = Math.max(...D.points.map(x => x.value)) || 1;
const box = document.getElementById('chart');
D.points.forEach(pt => {
  const col = document.createElement('div');
  col.className = 'col';
  col.innerHTML = '<div class="val"></div><div class="b'
    + (pt.value === maxv ? ' max' : '') + '"></div><div class="xl"></div>';
  col.querySelector('.xl').textContent = pt.label;
  box.appendChild(col);
});
const easeOut = t => 1 - Math.pow(1 - t, 3);
const seg = (p, a, b) => Math.min(1, Math.max(0, (p - a) / (b - a)));
function setProgress(p) {
  document.getElementById('title').style.opacity = seg(p, 0, 0.12);
  document.getElementById('unit').style.opacity = seg(p, 0.05, 0.18);
  const n = D.points.length;
  const slot = 0.55 / n;
  document.querySelectorAll('.col').forEach((col, i) => {
    const k = easeOut(seg(p, 0.12 + i * slot, 0.12 + i * slot + 0.3));
    const v = D.points[i].value;
    col.querySelector('.b').style.height = (330 * (v / maxv) * k) + 'px';
    const vl = col.querySelector('.val');
    vl.style.opacity = seg(p, 0.12 + i * slot + 0.1, 0.12 + i * slot + 0.3);
    vl.textContent = Math.round(v * k).toLocaleString('en-US');
  });
}
setProgress(0);
</script></body></html>"""


def _parse_mixed_number(val: str) -> float:
    """「1萬1400」→11400、「6000萬」→60000000、「2億3000萬」→230000000、「0.85」→0.85"""
    s = re.sub(r"[^0-9.萬億]", "", val or "")
    total = 0.0
    for unit, mult in (("億", 1e8), ("萬", 1e4)):
        if unit in s:
            head, s = s.split(unit, 1)
            try:
                total += float(head or 0) * mult
            except ValueError:
                pass
    try:
        total += float(s) if s else 0.0
    except ValueError:
        pass
    return total


def render_stat_card(stat: dict, out_path: Path, dur: float = DUR) -> bool:
    """
    stat = {"value": "760", "unit": "架次", "label": "航班取消", "note": "颱風巴威影響"}
    成功回 True；任何失敗回 False（插卡是加分項，不擋產製主流程）。
    """
    return render_card({**(stat or {}), "type": "stat"}, out_path)


def render_card(card: dict, out_path: Path) -> bool:
    """
    通用資訊卡渲染：card["type"] ∈ stat/timeline/alert/chart，秒數依 DUR_BY_TYPE。
    成功回 True；任何失敗回 False（插卡是加分項，不擋產製主流程）。
    """
    ctype = (card or {}).get("type", "stat")
    dur = card_duration(card)
    if ctype == "timeline":
        steps = [str(s) for s in (card.get("steps") or []) if str(s).strip()][:4]
        if len(steps) < 2:
            return False
        html = _HTML_TIMELINE.replace("%DATA%", json.dumps(
            {"title": str(card.get("title", "")), "steps": steps}, ensure_ascii=False))
    elif ctype == "alert":
        html = _HTML_ALERT.replace("%DATA%", json.dumps(
            {"icon": str(card.get("icon", "")), "headline": str(card.get("headline", "")),
             "sub": str(card.get("sub", ""))}, ensure_ascii=False))
    elif ctype == "chart":
        pts = [{"label": str(p.get("label", "")), "value": _parse_mixed_number(str(p.get("value", 0)))}
               for p in (card.get("points") or []) if isinstance(p, dict)][:6]
        if len(pts) < 3:
            return False
        html = _HTML_CHART.replace("%DATA%", json.dumps(
            {"title": str(card.get("title", "")), "unit": str(card.get("unit", "")),
             "points": pts}, ensure_ascii=False))
    else:   # stat
        val = str(card.get("value", ""))
        if not val.strip():
            return False
        html = _HTML.replace("%DATA%", json.dumps({
            "value": val,
            "unit": str(card.get("unit", "")),
            "label": str(card.get("label", "")),
            "note": str(card.get("note", "")),
            "target": _parse_mixed_number(val),
            "mode": ("yi" if "億" in val else ("wan" if "萬" in val else "plain")),
            "isInt": "." not in val,
        }, ensure_ascii=False))
    return _render_html_card(html, out_path, dur)


def _render_html_card(html: str, out_path: Path, dur: float) -> bool:
    frames_dir = TMP / "_statcard_frames"
    try:
        from playwright.sync_api import sync_playwright
        shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        page_file = frames_dir / "card.html"
        page_file.write_text(html, encoding="utf-8")

        n = int(dur * FPS)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 540, "height": 960},
                                    device_scale_factor=2)   # 截出 1080x1920
            page.goto(page_file.as_uri())
            page.wait_for_timeout(150)   # 等字型載入
            for i in range(n):
                p = min(1.0, i / (n * 0.82))   # 尾端 ~0.5 秒定格，數字停穩讓人看清
                page.evaluate(f"setProgress({p:.4f})")
                page.screenshot(path=str(frames_dir / f"f{i:04d}.png"))
            browser.close()

        r = subprocess.run(
            [_ffmpeg_exe(), "-y", "-framerate", str(FPS),
             "-i", str(frames_dir / "f%04d.png"),
             "-vf", f"scale={W}:{H}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", str(out_path)],
            capture_output=True, timeout=120)
        return r.returncode == 0 and out_path.exists()
    except Exception as e:
        print(f"statcard render failed: {e}")
        return False
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


if __name__ == "__main__":
    ok = render_stat_card(
        {"value": "760", "unit": "架次", "label": "航班取消", "note": "颱風巴威影響桃園機場"},
        Path(r"D:\VideoAI\tmp\_statcard_test.mp4"))
    print("OK" if ok else "FAILED")
