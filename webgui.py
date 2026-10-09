#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DJI-LogFix 网页界面
===================

在本机起一个小服务，用浏览器操作：
  * 浏览/选择素材文件夹或文件
  * 预览套 LUT 前后的画面对比
  * 批量还原，实时进度

只在 127.0.0.1 上监听，不对外网开放。
源素材全程只读。
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from djilogfix import (  # noqa: E402
    APP_DIR, FFMPEG, DEFAULT_OUT, VIDEO_EXT,
    probe, fmt_size, fmt_dur,
    list_luts, transcode, out_name,
)

HOST = "127.0.0.1"
PORT = 8777

# --------------------------------------------------------------------------
# 全局任务状态（单任务，够用且简单）
# --------------------------------------------------------------------------

JOB = {
    "running": False,
    "cancel": threading.Event(),
    "total": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "current": "",
    "file_pct": 0.0,
    "file_cur": 0.0,
    "file_total": 0.0,
    "logs": [],
    "outdir": "",
    "started": 0.0,
    "finished": 0.0,
    "elapsed_total": 0.0,
}
JOB_LOCK = threading.Lock()


def jlog(msg: str):
    with JOB_LOCK:
        JOB["logs"].append(msg)
        if len(JOB["logs"]) > 400:
            del JOB["logs"][:200]


# --------------------------------------------------------------------------
# 素材扫描
# --------------------------------------------------------------------------

SCAN_CACHE = {}


def scan_dir(path: str, recursive: bool = True) -> list:
    """扫描目录下的视频，返回带元数据的列表。"""
    p = Path(path).expanduser()
    if not p.is_dir():
        return []

    files = []
    it = p.rglob("*") if recursive else p.glob("*")
    for f in it:
        try:
            if f.is_file() and f.suffix.lower() in VIDEO_EXT and not f.name.startswith("._"):
                files.append(f)
        except OSError:
            continue
    files.sort()

    # 并发探测，109 个文件也就一两秒
    def one(f):
        inf = probe(f)
        return (str(f), inf) if inf else None

    out = []
    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2)) as ex:
        for r in ex.map(one, files):
            if r:
                out.append(r)
    return out


# --------------------------------------------------------------------------
# 原生路径选择器（osascript）
# --------------------------------------------------------------------------

def native_pick(kind: str = "folder") -> str:
    """调用 macOS 原生选择框拿路径。kind: folder | files"""
    if kind == "folder":
        script = 'POSIX path of (choose folder with prompt "选择素材文件夹")'
    else:
        script = (
            'set fs to (choose file with prompt "选择视频文件" '
            'of type {"public.movie"} with multiple selections allowed)\n'
            'set out to ""\n'
            'repeat with f in fs\n'
            '  set out to out & POSIX path of f & "\\n"\n'
            'end repeat\n'
            'return out'
        )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return ""
        return r.stdout.strip().rstrip("/")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 预览
# --------------------------------------------------------------------------

def make_preview(src: str, lut: str, t: float, height: int, eq: dict) -> dict:
    """生成套 LUT 前后的两张图，返回相对 URL。"""
    outp = APP_DIR / "预览"
    outp.mkdir(exist_ok=True)
    stamp = str(int(time.time() * 1000))
    raw = outp / f"{stamp}_before.png"
    fixed = outp / f"{stamp}_after.png"

    inf = probe(Path(src))
    if not inf:
        return {"ok": False, "error": "无法解析该视频"}

    # 预览最多 1080p；且绝不放大超过源高度
    prev_h = min(height or 1080, 1080)
    if inf["h"] and prev_h > inf["h"]:
        prev_h = inf["h"]
    base = [str(FFMPEG), "-hide_banner", "-v", "error", "-y"]
    if inf["w"] >= 3000:
        base += ["-hwaccel", "videotoolbox"]

    def build(dst, apply_lut):
        vf = [f"scale=-2:{prev_h}"]
        if apply_lut and lut:
            vf.append(f"lut3d=file='{lut}'")
        parts = []
        if abs(eq.get("brightness", 0.0)) > 1e-6:
            parts.append(f"brightness={eq['brightness']:.4f}")
        if abs(eq.get("contrast", 1.0) - 1.0) > 1e-6:
            parts.append(f"contrast={eq['contrast']:.4f}")
        if abs(eq.get("saturation", 1.0) - 1.0) > 1e-6:
            parts.append(f"saturation={eq['saturation']:.4f}")
        if parts:
            vf.append("eq=" + ":".join(parts))
        cmd = base + ["-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                      "-vf", ",".join(vf), str(dst)]
        return cmd

    tasks = [(build(raw, False), raw), (build(fixed, True), fixed)]
    for cmd, dst in tasks:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not dst.exists():
            return {"ok": False, "error": (r.stderr or "生成失败")[:300]}

    return {
        "ok": True,
        "before": f"/preview/{raw.name}",
        "after": f"/preview/{fixed.name}",
        "time": t,
        "width": inf["w"],
        "height": inf["h"],
        "dur": inf["dur"],
    }


# --------------------------------------------------------------------------
# 后台转码
# --------------------------------------------------------------------------

def run_job(items: list, opts: dict, outdir: str, overwrite: bool):
    with JOB_LOCK:
        JOB.update({
            "running": True, "total": len(items), "done": 0, "failed": 0,
            "skipped": 0, "current": "", "file_pct": 0.0, "file_cur": 0.0,
            "file_total": 0.0, "logs": [], "outdir": outdir,
            "started": time.time(), "finished": 0.0, "elapsed_total": 0.0,
        })
    JOB["cancel"].clear()

    outdir_p = Path(outdir).expanduser()
    outdir_p.mkdir(parents=True, exist_ok=True)

    # 空间预检
    try:
        import shutil as sh
        need = sum(i.get("size", 0) for i in items) * 1.1
        free = sh.disk_usage(outdir_p).free
        if free < need:
            jlog(f"⚠ 输出盘可用 {fmt_size(free)}，估计需要约 {fmt_size(need)}，空间可能不足")
    except Exception:
        pass

    t_all = time.time()
    for idx, it in enumerate(items, 1):
        if JOB["cancel"].is_set():
            jlog("已停止")
            break
        src = Path(it["path"])
        inf = it.get("info", {})
        dst = outdir_p / out_name(src, opts)

        with JOB_LOCK:
            JOB["current"] = src.name
            JOB["file_pct"] = 0.0
            JOB["file_cur"] = 0.0
            JOB["file_total"] = inf.get("dur", 0) or 0

        if dst.exists() and not overwrite:
            jlog(f"[{idx}/{len(items)}] 跳过（已存在）{dst.name}")
            with JOB_LOCK:
                JOB["skipped"] += 1
            continue

        jlog(f"[{idx}/{len(items)}] {src.name}  ({fmt_dur(inf.get('dur', 0))}, "
             f"{fmt_size(inf.get('size', 0))})")
        t0 = time.time()

        def prog(pct, cur, total):
            with JOB_LOCK:
                JOB["file_pct"] = pct
                JOB["file_cur"] = cur

        good, err = transcode(src, dst, opts, on_progress=prog, cancel=JOB["cancel"])
        if good:
            el = max(time.time() - t0, 0.001)
            speed = (inf.get("dur", 0) / el) if inf.get("dur") else 0
            jlog(f"    ✓ {dst.name}  {fmt_size(dst.stat().st_size)}, "
                 f"{el:.0f}s, {speed:.2f}x")
            with JOB_LOCK:
                JOB["done"] += 1
        else:
            jlog(f"    ✗ 失败: {err[:300]}")
            with JOB_LOCK:
                JOB["failed"] += 1
        with JOB_LOCK:
            JOB["file_pct"] = 0.0

    total_el = time.time() - t_all
    with JOB_LOCK:
        JOB["elapsed_total"] = total_el
        JOB["running"] = False
        JOB["finished"] = time.time()
        JOB["current"] = ""
    jlog(f"=== 结束：成功 {JOB['done']}，跳过 {JOB['skipped']}，失败 {JOB['failed']}，"
         f"总耗时 {total_el:.0f}s ===")
    jlog(f"输出目录：{outdir_p}")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DJILogFix/1.0"

    def log_message(self, *a):
        pass

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- routes ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path == "/":
            return self.send_bytes(INDEX_HTML.encode("utf-8"))

        if path == "/api/env":
            return self.send_json({
                "cwd": str(Path.cwd()),
                "home": str(Path.home()),
                "default_out": str(DEFAULT_OUT),
                "volumes": [str(p) for p in sorted(Path("/Volumes").iterdir())]
                if Path("/Volumes").is_dir() else [],
                "cpu": os.cpu_count() or 0,
                "luts": [
                    {"name": n, "path": str(p), "cat": c}
                    for n, p, c in list_luts()
                ],
            })

        if path == "/api/scanfiles":
            d = (q.get("paths") or [""])[0]
            wanted = [p for p in d.split("\n") if p.strip()]
            out = []
            def one(p):
                f = Path(p)
                if not (f.is_file() and f.suffix.lower() in VIDEO_EXT):
                    return None
                inf = probe(f)
                return (str(f), inf) if inf else None
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2)) as ex:
                for r in ex.map(one, wanted):
                    if r:
                        out.append(r)
            return self.send_json({
                "ok": True,
                "count": len(out),
                "files": [{"path": p, "name": Path(p).name, **inf} for p, inf in out],
            })

        if path == "/api/scan":
            d = (q.get("dir") or [""])[0]
            if not d:
                return self.send_json({"ok": False, "error": "缺少目录"})
            t0 = time.time()
            items = scan_dir(d)
            return self.send_json({
                "ok": True,
                "dir": d,
                "count": len(items),
                "elapsed": round(time.time() - t0, 2),
                "files": [
                    {"path": p, "name": Path(p).name, **inf} for p, inf in items
                ],
            })

        if path == "/api/pick":
            kind = (q.get("kind") or ["folder"])[0]
            p = native_pick(kind)
            return self.send_json({"ok": bool(p), "path": p})

        if path == "/api/status":
            with JOB_LOCK:
                snap = dict(JOB)
                snap.pop("cancel", None)
            snap["logs"] = snap["logs"][-200:]
            if snap.get("running") and snap.get("started"):
                snap["elapsed"] = time.time() - snap["started"]
            elif snap.get("finished") and snap.get("started"):
                snap["elapsed"] = snap["elapsed_total"]
            else:
                snap["elapsed"] = 0
            return self.send_json(snap)

        if path.startswith("/preview/"):
            name = os.path.basename(path[len("/preview/"):])
            f = APP_DIR / "预览" / name
            if f.exists() and f.suffix == ".png":
                return self.send_bytes(f.read_bytes(), "image/png")
            return self.send_json({"ok": False, "error": "not found"}, 404)

        if path == "/api/lutinfo":
            lp = (q.get("path") or [""])[0]
            f = Path(lp)
            if not f.exists():
                return self.send_json({"ok": False})
            size = 0
            for line in f.read_text(errors="ignore").splitlines():
                if line.strip().startswith("LUT_3D_SIZE"):
                    try:
                        size = int(line.split()[1])
                    except Exception:
                        pass
            return self.send_json({"ok": True, "size": size, "path": str(f)})

        self.send_json({"ok": False, "error": "unknown"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/preview":
            b = self.read_json()
            try:
                r = make_preview(
                    b["path"],
                    b.get("lut") or "",
                    float(b.get("time") or 0),
                    int(b.get("height") or 1080),
                    b.get("eq") or {},
                )
            except Exception as e:
                r = {"ok": False, "error": str(e)[:300]}
            return self.send_json(r)

        if u.path == "/api/start":
            if JOB.get("running"):
                return self.send_json({"ok": False, "error": "已有任务在运行"})
            b = self.read_json()
            items = b.get("items") or []
            if not items:
                return self.send_json({"ok": False, "error": "没有选择素材"})
            opts = b.get("opts") or {}
            outdir = b.get("outdir") or str(DEFAULT_OUT)
            threading.Thread(
                target=run_job,
                args=(items, opts, outdir, bool(b.get("overwrite"))),
                daemon=True,
            ).start()
            return self.send_json({"ok": True})

        if u.path == "/api/stop":
            JOB["cancel"].set()
            return self.send_json({"ok": True})

        if u.path == "/api/getluts":
            # LUT 版权归 DJI，仓库不附带，这里按需从官方 CDN 下载
            script = APP_DIR / "tools" / "get_luts.py"
            if not script.exists():
                return self.send_json({"ok": False, "error": "缺少 tools/get_luts.py"})
            try:
                r = subprocess.run(
                    [sys.executable, str(script)],
                    capture_output=True, text=True, timeout=600,
                )
                return self.send_json({
                    "ok": r.returncode == 0,
                    "out": (r.stdout or "")[-4000:],
                    "err": (r.stderr or "")[-1000:],
                    "luts": [
                        {"name": n, "path": str(p), "cat": c}
                        for n, p, c in list_luts()
                    ],
                })
            except Exception as e:
                return self.send_json({"ok": False, "error": str(e)[:300]})

        if u.path == "/api/reveal":
            b = self.read_json()
            d = b.get("dir") or ""
            if d and Path(d).exists():
                subprocess.run(["open", d], capture_output=True)
                return self.send_json({"ok": True})
            return self.send_json({"ok": False})

        self.send_json({"ok": False, "error": "unknown"}, 404)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DJI LogFix — Log 素材批量还原</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --text:#e8eaf0; --dim:#9aa3b2; --accent:#4c8dff; --accent2:#2f6fe0;
    --ok:#3ecf8e; --warn:#e8b84b; --err:#ff6b6b; --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
  header{display:flex;align-items:baseline;gap:12px;margin-bottom:4px}
  h1{font-size:19px;margin:0;letter-spacing:.2px}
  .sub{color:var(--dim);font-size:12.5px}
  .card{background:var(--panel);border:1px solid var(--line);
    border-radius:var(--radius);padding:16px;margin-top:14px}
  .card h2{font-size:13px;margin:0 0 12px;color:var(--dim);
    font-weight:600;letter-spacing:.4px;text-transform:uppercase}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .row+.row{margin-top:9px}
  input[type=text],select{background:var(--panel2);border:1px solid var(--line);
    color:var(--text);border-radius:7px;padding:8px 10px;font-size:13px;outline:none}
  input[type=text]:focus,select:focus{border-color:var(--accent)}
  input[type=text]{flex:1;min-width:200px}
  button{background:var(--panel2);border:1px solid var(--line);color:var(--text);
    border-radius:7px;padding:8px 13px;font-size:13px;cursor:pointer;
    transition:.13s;white-space:nowrap}
  button:hover{background:#252a34;border-color:#3d4553}
  button:disabled{opacity:.42;cursor:not-allowed}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.primary:hover{background:var(--accent2);border-color:var(--accent2)}
  button.danger{color:var(--err)}
  button.sm{padding:5px 10px;font-size:12px}
  label.lbl{color:var(--dim);font-size:12.5px;min-width:74px;display:inline-block}
  .pill{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);
    border:1px solid var(--line);border-radius:20px;padding:5px 12px;font-size:12.5px;
    cursor:pointer;user-select:none}
  .pill input{accent-color:var(--accent);margin:0}
  .pill.on{border-color:var(--accent);color:#cfe0ff}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{position:sticky;top:0;background:var(--panel2);color:var(--dim);
    text-align:left;padding:8px;font-weight:500;border-bottom:1px solid var(--line);
    cursor:pointer;white-space:nowrap}
  td{padding:6px 8px;border-bottom:1px solid #21252e;white-space:nowrap}
  tr:hover td{background:#1b1f27}
  .tw{max-height:330px;overflow:auto;border:1px solid var(--line);
    border-radius:8px;margin-top:10px}
  td.n{max-width:420px;overflow:hidden;text-overflow:ellipsis}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .dim{color:var(--dim)}
  .bar{height:6px;background:#22262f;border-radius:4px;overflow:hidden}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#7aa7ff);
    width:0;transition:width .2s}
  .log{background:#0c0e12;border:1px solid var(--line);border-radius:8px;
    padding:10px;height:170px;overflow:auto;font-family:ui-monospace,Menlo,monospace;
    font-size:11.5px;line-height:1.6;color:#c3cad6;white-space:pre-wrap}
  .cmp{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .cmp figure{margin:0}
  .cmp img{width:100%;border-radius:8px;border:1px solid var(--line);display:block}
  .cmp figcaption{color:var(--dim);font-size:12px;margin-top:6px;text-align:center}
  .st{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--dim)}
  .st.run{background:var(--ok);animation:p 1.1s infinite}
  @keyframes p{50%{opacity:.25}}
  .kv{display:flex;gap:18px;flex-wrap:wrap;color:var(--dim);font-size:12.5px}
  .kv b{color:var(--text);font-weight:600}
  .hint{color:var(--dim);font-size:12px;margin-top:8px}
  .warnbox{background:#2a2416;border:1px solid #5a4a1e;color:#e8d9a8;
    border-radius:8px;padding:9px 12px;font-size:12.5px;margin-top:10px}
  input[type=range]{accent-color:var(--accent);width:130px;vertical-align:middle}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media(max-width:820px){.grid2,.cmp{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>DJI LogFix</h1>
    <span class="sub">DJI D-Log / D-Log M 素材批量还原为 Rec.709 &nbsp;·&nbsp; 全程只读原片</span>
  </header>

  <!-- 1 素材 -->
  <div class="card">
    <h2>1 · 选择素材</h2>
    <div class="row">
      <input type="text" id="dir" placeholder="输入素材文件夹路径，例如 /Volumes/Untitled/DCIM/DJI_001">
      <button onclick="pick('folder')">浏览文件夹…</button>
      <button onclick="pick('files')">选择文件…</button>
      <button class="primary" onclick="scan()">扫描</button>
    </div>
    <div class="row" id="quick"></div>
    <div class="hint" id="scanmsg">扫描只读取元数据，不会改动或删除任何文件。</div>
    <div class="tw" id="tw" style="display:none">
      <table>
        <thead><tr>
          <th style="width:34px"><input type="checkbox" id="all" onchange="toggleAll(this)"></th>
          <th onclick="sortBy('name')">文件名</th>
          <th onclick="sortBy('w')" style="width:96px">分辨率</th>
          <th onclick="sortBy('fps')" style="width:66px">帧率</th>
          <th onclick="sortBy('dur')" style="width:82px">时长</th>
          <th onclick="sortBy('size')" style="width:88px">大小</th>
          <th style="width:150px">编码</th>
        </tr></thead>
        <tbody id="tb"></tbody>
      </table>
    </div>
  </div>

  <!-- 2 参数 -->
  <div class="card">
    <h2>2 · 处理参数</h2>
    <div class="row">
      <label class="lbl">LUT</label>
      <select id="lut" style="flex:1;min-width:280px"></select>
      <button class="sm" onclick="lutInfo()">查看</button>
      <button class="sm" id="getlut" onclick="getLuts()">下载官方 LUT</button>
      <span class="dim" id="lutinfo"></span>
    </div>
    <div class="warnbox" id="lutwarn" style="display:none"></div>
    <div class="warnbox" id="lutmissing" style="display:none"></div>

    <div class="grid2" style="margin-top:14px">
      <div>
        <div class="row"><label class="lbl">输出分辨率</label>
          <select id="height">
            <option value="2160">4K 2160p（保持原样）</option>
            <option value="1440">2.7K 1440p</option>
            <option value="1080" selected>1080p（快约 4 倍）</option>
            <option value="720">720p（最省空间）</option>
          </select>
        </div>
        <div class="row"><label class="lbl">画质</label>
          <select id="quality">
            <option value="high" selected>高（接近原片）</option>
            <option value="medium">中</option>
            <option value="small">小（省空间）</option>
          </select>
        </div>
        <div class="row"><label class="lbl">编码器</label>
          <select id="encoder">
            <option value="videotoolbox" selected>硬件加速（快很多）</option>
            <option value="x265">x265 软件（体积更小）</option>
          </select>
        </div>
        <div class="row"><label class="lbl">音频</label>
          <label class="pill" id="p_audio"><input type="checkbox" id="audio" checked onchange="pill('p_audio',this)">保留音频</label>
          <label class="pill" id="p_data" title="DJI 的 djmd 陀螺仪轨无法放进 MP4，勾选后输出改为 .mov"><input type="checkbox" id="data" onchange="pill('p_data',this)">保留陀螺仪数据轨（.mov）</label>
        </div>
      </div>
      <div>
        <div class="row"><label class="lbl">亮度</label>
          <input type="range" id="brightness" min="-0.25" max="0.25" step="0.01" value="0"
                 oninput="v('brightness')"><span class="mono dim" id="v_brightness" style="width:46px">0.00</span>
        </div>
        <div class="row"><label class="lbl">对比度</label>
          <input type="range" id="contrast" min="0.6" max="1.6" step="0.01" value="1"
                 oninput="v('contrast')"><span class="mono dim" id="v_contrast" style="width:46px">1.00</span>
        </div>
        <div class="row"><label class="lbl">饱和度</label>
          <input type="range" id="saturation" min="0.4" max="1.8" step="0.01" value="1"
                 oninput="v('saturation')"><span class="mono dim" id="v_saturation" style="width:46px">1.00</span>
        </div>
        <div class="row" style="margin-top:12px">
          <button onclick="resetEq()">重置微调</button>
          <button class="primary" onclick="preview()">预览对比帧</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 3 输出 -->
  <div class="card">
    <h2>3 · 输出位置</h2>
    <div class="row">
      <input type="text" id="outdir">
      <button onclick="pickOut()">更改…</button>
      <label class="pill" id="p_ow"><input type="checkbox" id="ow" onchange="pill('p_ow',this)">覆盖同名输出</label>
    </div>
    <div class="hint">输出文件名统一加 <span class="mono">_Rec709</span> 后缀，不会覆盖原始素材。</div>
  </div>

  <!-- 预览 -->
  <div class="card" id="pvcard" style="display:none">
    <h2>预览对比（左：原始 Log　右：套用 LUT 后）</h2>
    <div class="cmp">
      <figure><img id="pv_before"><figcaption>原始 D-Log M</figcaption></figure>
      <figure><img id="pv_after"><figcaption>还原后 Rec.709</figcaption></figure>
    </div>
    <div class="hint" id="pvmsg"></div>
  </div>

  <!-- 执行 -->
  <div class="card">
    <h2>4 · 开始处理</h2>
    <div class="row">
      <button class="primary" id="run" onclick="start()" style="padding:10px 22px">开始批量还原</button>
      <button class="danger" id="stop" onclick="stopJob()" disabled>停止</button>
      <button class="sm" onclick="openOut()">打开输出文件夹</button>
      <span class="kv" style="margin-left:auto" id="kvs"></span>
    </div>
    <div style="margin-top:13px">
      <div class="row" style="justify-content:space-between;margin-bottom:6px">
        <span class="sub"><span class="st" id="dot"></span> <span id="cur">空闲</span></span>
        <span class="sub mono" id="pct"></span>
      </div>
      <div class="bar"><i id="b1"></i></div>
      <div class="hint" style="margin-bottom:8px" id="overall"></div>
      <div class="log" id="log">等待开始…</div>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let FILES = [], SORT = {k:'name', d:1}, POLL = null, ENV = {};

function pill(id, el){ $(id).classList.toggle('on', el.checked); }
function v(k){ $('v_'+k).textContent = parseFloat($(k).value).toFixed(2); }
function resetEq(){
  $('#brightness').value=0; $('#contrast').value=1; $('#saturation').value=1;
  ['brightness','contrast','saturation'].forEach(v);
}
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fs = n => { n=+n||0; const u=['B','KB','MB','GB','TB']; let i=0;
  while(n>=1024 && i<u.length-1){n/=1024;i++} return (i<2? n.toFixed(0): n.toFixed(2))+' '+u[i]; };
const fd = s => { s=Math.round(+s||0); const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=s%60;
  return h? `${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}` : `${m}:${String(x).padStart(2,'0')}`; };

async function api(p, body){
  const o = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
  const r = await fetch(p, o); return r.json();
}

async function boot(){
  ENV = await api('/api/env');
  $('#outdir').value = ENV.default_out;
  fillLuts(ENV.luts);
  $('#quick').innerHTML = ENV.volumes.map(v =>
    `<button class="sm" onclick="quick('${esc(v)}')">${esc(v)}</button>`).join('');
  updKv();
}
function quick(v){ $('#dir').value = v; scan(); }

function fillLuts(luts){
  ENV.luts = luts || [];
  $('#lut').innerHTML = ENV.luts.map(l =>
    `<option value="${esc(l.path)}" data-cat="${esc(l.cat)}">[${esc(l.cat)}] ${esc(l.name)}</option>`).join('');
  $('#lut').onchange = lutWarn;
  const hasRestore = ENV.luts.some(l => (l.cat||'').includes('D-LogM'));
  const miss = $('#lutmissing');
  if(!ENV.luts.length || !hasRestore){
    miss.style.display = 'block';
    miss.innerHTML = '⚠ 还没安装 DJI 官方还原 LUT，无法还原色彩。'
      + '点上方「<b>下载官方 LUT</b>」自动获取（约 7 MB，来自 DJI 官方 CDN）；'
      + '也可以手动到 DJI 下载中心搜索「OSMO Action 5 Pro D-Log M to Rec.709 LUT」，'
      + '把 .cube 文件放进 <span class="mono">LUTs/官方D-LogM/</span>。';
  } else { miss.style.display = 'none'; }
  lutWarn();
}

async function getLuts(){
  const b = $('#getlut');
  b.disabled = true; b.textContent = '下载中…';
  $('#lutinfo').textContent = '正在从 DJI 官方 CDN 获取…';
  const r = await api('/api/getluts', {});
  b.disabled = false; b.textContent = '下载官方 LUT';
  if(r.ok){
    fillLuts(r.luts);
    $('#lutinfo').textContent = `已安装 ${r.luts.length} 个 LUT`;
  } else {
    alert('下载失败：' + (r.error || r.err || '请检查网络'));
    $('#lutinfo').textContent = '下载失败';
  }
}
function updKv(){
  const n = FILES.filter(f=>f._sel).length;
  const sz = FILES.filter(f=>f._sel).reduce((a,b)=>a+b.size,0);
  const du = FILES.filter(f=>f._sel).reduce((a,b)=>a+b.dur,0);
  $('#kvs').innerHTML = n
    ? `已选 <b>${n}</b> 个 · <b>${fs(sz)}</b> · <b>${fd(du)}</b>` : '未选择素材';
}

async function pick(kind){
  $('#scanmsg').textContent = kind==='folder' ? '请在弹出的系统窗口中选择文件夹…' : '请在弹出的系统窗口中选择文件…';
  const r = await api('/api/pick?kind='+kind);
  if(!r.ok){ $('#scanmsg').textContent = '未选择（或已取消）'; return; }
  if(kind==='folder'){ $('#dir').value = r.path; scan(); }
  else {
    $('#dir').value = '';
    const list = r.path.split('\n').map(s=>s.trim()).filter(Boolean);
    $('#scanmsg').textContent = `已选择 ${list.length} 个文件，正在读取信息…`;
    const s = await api('/api/scanfiles?paths='+encodeURIComponent(list.join('\n')));
    if(s.ok && s.count){
      FILES = s.files.map(f=>({...f,_sel:true}));
      render();
      $('#scanmsg').innerHTML = `已载入 <b>${s.count}</b> 个视频文件。`;
    } else {
      $('#scanmsg').textContent = '这些文件无法解析为视频。';
    }
  }
}
async function pickOut(){
  const r = await api('/api/pick?kind=folder');
  if(r.ok) $('#outdir').value = r.path;
}

async function scan(){
  const d = $('#dir').value.trim();
  if(!d){ $('#scanmsg').textContent = '请先填写或选择文件夹路径'; return; }
  $('#scanmsg').innerHTML = '正在扫描…';
  const r = await api('/api/scan?dir='+encodeURIComponent(d));
  if(!r.ok){ $('#scanmsg').textContent = '扫描失败：'+(r.error||''); return; }
  FILES = r.files.map(f=>({...f,_sel:true}));
  $('#scanmsg').innerHTML = `找到 <b>${r.count}</b> 个视频，用时 ${r.elapsed}s。`
    + (r.count? '默认全部勾选，可单独取消。' : '该目录下没有视频文件。');
  render();
}

function render(){
  const rows = [...FILES].sort((a,b)=>{
    let x=a[SORT.k], y=b[SORT.k];
    if(typeof x==='string') return x.localeCompare(y,'zh')*SORT.d;
    return ((x||0)-(y||0))*SORT.d;
  });
  $('#tb').innerHTML = rows.map(f=>{
    const i = FILES.indexOf(f);
    return `<tr>
      <td><input type="checkbox" ${f._sel?'checked':''} onchange="sel(${i},this)"></td>
      <td class="n" title="${esc(f.name)}">${esc(f.name)}</td>
      <td class="dim">${f.w}×${f.h}</td>
      <td class="dim">${Math.round(f.fps)}p</td>
      <td class="dim">${fd(f.dur)}</td>
      <td class="dim">${fs(f.size)}</td>
      <td class="dim mono" style="font-size:11.5px">${esc(f.codec)} ${esc(f.pix_fmt)}</td>
    </tr>`;
  }).join('');
  $('#tw').style.display = FILES.length ? 'block' : 'none';
  $('#all').checked = FILES.length>0 && FILES.every(f=>f._sel);
  updKv();
}
function sel(i,el){ FILES[i]._sel = el.checked; updKv(); }
function toggleAll(el){ FILES.forEach(f=>f._sel = el.checked); render(); }
function sortBy(k){ SORT.d = (SORT.k===k? -SORT.d : 1); SORT.k = k; render(); }

function lutWarn(){
  const o = $('#lut').selectedOptions[0];
  const cat = o ? o.dataset.cat : '';
  const w = $('#lutwarn');
  if(cat && cat.includes('风格化')){
    w.style.display='block';
    w.innerHTML = '⚠ 这个 LUT 的输入是 <b>Rec.709</b>，不是 Log。'
      + '直接套在 D-Log M 素材上会偏暗、色彩不对。'
      + '请先选「官方D-LogM」分类里的还原 LUT；这个风格化 LUT 适合在还原之后再叠加使用。';
  } else { w.style.display='none'; }
}
async function lutInfo(){
  const r = await api('/api/lutinfo?path='+encodeURIComponent($('#lut').value));
  $('#lutinfo').textContent = r.ok ? `LUT 网格 ${r.size}³` : '无法读取';
}

function eqObj(){
  return { brightness:+$('#brightness').value, contrast:+$('#contrast').value, saturation:+$('#saturation').value };
}

async function preview(){
  const sel = FILES.filter(f=>f._sel);
  if(!sel.length){ alert('请先选择素材'); return; }
  const f = sel[0];
  const t = f.dur ? Math.min(f.dur*0.3, 20) : 0;
  $('#pvcard').style.display='block';
  $('#pvmsg').textContent = '正在生成对比帧…（4K 素材约需 5-15 秒）';
  const r = await api('/api/preview', {
    path:f.path, lut:$('#lut').value, time:t,
    height:+$('#height').value||1080, eq:eqObj()
  });
  if(!r.ok){ $('#pvmsg').textContent = '预览失败：'+(r.error||''); return; }
  $('#pv_before').src = r.before + '?t=' + Date.now();
  $('#pv_after').src  = r.after  + '?t=' + Date.now();
  $('#pvmsg').textContent = `取自 ${f.name} 第 ${fd(r.time)} 处 · 原片 ${r.width}×${r.height}`;
  $('#pvcard').scrollIntoView({behavior:'smooth', block:'nearest'});
}

async function start(){
  const sel = FILES.filter(f=>f._sel);
  if(!sel.length){ alert('请先选择素材'); return; }
  const items = sel.map(f=>({path:f.path, name:f.name, size:f.size, info:f}));
  const opts = {
    lut: $('#lut').value,
    height: +$('#height').value||0,
    quality: $('#quality').value,
    encoder: $('#encoder').value,
    brightness: +$('#brightness').value,
    contrast: +$('#contrast').value,
    saturation: +$('#saturation').value,
    no_audio: !$('#audio').checked,
    keep_data: $('#data').checked,
    filter_threads: ENV.cpu || 4,
  };
  const r = await api('/api/start', {
    items, opts, outdir: $('#outdir').value, overwrite: $('#ow').checked
  });
  if(!r.ok){ alert(r.error||'无法启动'); return; }
  $('#run').disabled = true; $('#stop').disabled = false;
  $('#log').textContent = '';
  if(POLL) clearInterval(POLL);
  POLL = setInterval(tick, 500);
  tick();
}

async function stopJob(){ await api('/api/stop', {}); }
async function openOut(){ await api('/api/reveal', {dir: $('#outdir').value}); }

let LASTLOG = 0;
async function tick(){
  const s = await api('/api/status');
  $('#dot').className = 'st' + (s.running? ' run':'');
  if(s.running){
    $('#cur').textContent = s.current ? `处理中：${s.current}` : '准备中…';
  } else if(s.total){
    $('#cur').textContent = `结束 · 成功 ${s.done} · 跳过 ${s.skipped} · 失败 ${s.failed}`;
  } else { $('#cur').textContent = '空闲'; }

  $('#b1').style.width = (s.file_pct||0) + '%';
  if(s.running && s.file_total){
    $('#pct').textContent = `${Math.round(s.file_pct)}%  ${fd(s.file_cur)}/${fd(s.file_total)}`;
  } else { $('#pct').textContent = ''; }

  if(s.total){
    const all = ((s.done + s.skipped + s.failed)/s.total*100).toFixed(0);
    $('#overall').textContent =
      `总进度 ${s.done + s.skipped + s.failed}/${s.total}（${all}%）· 已用时 ${fd(s.elapsed)}`;
  } else { $('#overall').textContent = ''; }

  if(s.logs && s.logs.length !== LASTLOG){
    LASTLOG = s.logs.length;
    const box = $('#log');
    box.textContent = s.logs.join('\n');
    box.scrollTop = box.scrollHeight;
  }
  if(!s.running && s.finished){
    $('#run').disabled = false; $('#stop').disabled = true;
    if(POLL){ clearInterval(POLL); POLL = null; }
  }
}
boot();
</script>
</body>
</html>
"""


def main():
    if not FFMPEG.exists():
        print(f"错误：找不到 ffmpeg：{FFMPEG}", file=sys.stderr)
        return 2
    # 端口被占就顺延
    global PORT
    for p in range(PORT, PORT + 20):
        try:
            srv = ThreadingHTTPServer((HOST, p), Handler)
            PORT = p
            break
        except OSError:
            continue
    else:
        print("错误：找不到可用端口", file=sys.stderr)
        return 2

    url = f"http://{HOST}:{PORT}/"
    print("=" * 58)
    print("  DJI LogFix 已启动")
    print(f"  请在浏览器打开：{url}")
    print("  源素材全程只读，不会修改或删除任何原文件")
    print("  关闭本窗口即可退出")
    print("=" * 58)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
