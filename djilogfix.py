#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DJI-LogFix —— DJI Log 素材批量还原工具
=====================================

给 DJI Osmo Action / Pocket / Mavic 等设备拍摄的 D-Log M / D-Log 素材
批量套用官方 LUT，还原成正常对比度和色彩的 Rec.709 视频。

设计原则：
  * 只读源文件，绝不修改、移动、删除原始素材
  * 输出到独立目录，可选覆盖保护
  * 先预览后执行，参数可调

用法：
  python3 djilogfix.py                 # 图形界面（推荐）
  python3 djilogfix.py --cli ...       # 命令行
"""

import os
import re
import sys
import shutil
import subprocess
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
FFMPEG = APP_DIR / "tools" / "ffmpeg"
LUT_DIR = APP_DIR / "LUTs"
DEFAULT_OUT = APP_DIR / "输出"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".m4v", ".mts", ".avi"}


# --------------------------------------------------------------------------
# ffmpeg 探测
# --------------------------------------------------------------------------

def probe(path: Path) -> dict:
    """读取视频基本信息。返回 {} 表示不是可识别的视频。

    注意：这个静态 ffmpeg 包不含 ffprobe，所以直接解析 `ffmpeg -i` 的 stderr。
    """
    cmd = [str(FFMPEG), "-hide_banner", "-i", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return {}
    # ffmpeg -i 无输出文件时会返回 1，属正常；信息都在 stderr
    text = r.stderr or ""

    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    bitrate = 0
    m = re.search(r"Duration:.*?bitrate:\s*(\d+)\s*kb/s", text)
    if m:
        bitrate = int(m.group(1)) * 1000

    # 第一条视频流
    vline = None
    for line in text.splitlines():
        if re.search(r"Stream #\d+:\d+.*Video:", line):
            vline = line
            break
    if not vline:
        return {}

    w = h = 0
    m = re.search(r"(\d{2,5})x(\d{2,5})", vline)
    if m:
        w, h = int(m.group(1)), int(m.group(2))

    fps = 0.0
    m = re.search(r"([\d.]+)\s*fps", vline)
    if m:
        try:
            fps = float(m.group(1))
        except ValueError:
            pass

    codec = "?"
    m = re.search(r"Video:\s*([a-zA-Z0-9_]+)", vline)
    if m:
        codec = m.group(1)

    pix = "?"
    m = re.search(r"(yuv\w+|p0\d\w+|gbr\w+|nv\d+)", vline)
    if m:
        pix = m.group(1)

    size = 0
    try:
        size = path.stat().st_size
    except OSError:
        pass

    return {
        "w": w,
        "h": h,
        "fps": fps,
        "dur": dur,
        "codec": codec,
        "pix_fmt": pix,
        "size": size,
        "bitrate": bitrate,
        "ten_bit": ("10" in pix) or ("Main 10" in vline),
    }


def fmt_dur(sec: float) -> str:
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit not in ("B", "KB") else f"{n:.0f} {unit}"
        n /= 1024


# --------------------------------------------------------------------------
# LUT 库
# --------------------------------------------------------------------------

def list_luts() -> list:
    """返回 [(显示名, 路径, 分类)]，按目录组织。"""
    items = []
    if not LUT_DIR.is_dir():
        return items
    for sub in sorted(LUT_DIR.iterdir()):
        if not sub.is_dir():
            continue
        for f in sorted(sub.glob("*.cube")):
            items.append((f.stem, f, sub.name))
    # 根目录下直接放的 LUT 也带上
    for f in sorted(LUT_DIR.glob("*.cube")):
        items.append((f.stem, f, "其他"))
    return items


# --------------------------------------------------------------------------
# 转码核心
# --------------------------------------------------------------------------

def build_cmd(src: Path, dst: Path, opts: dict) -> list:
    """构造 ffmpeg 命令。opts 见 transcode()。"""
    vf = []

    # 1) 缩放（先缩放再做 LUT，能大幅提速）
    #    关键：绝不放大。目标高度一旦 >= 源高度就保持原样，
    #    否则 1080p 素材会被白白插值成 4K，既慢又损失画质。
    want_h = int(opts.get("height") or 0)
    src_h = int(opts.get("_src_h") or 0)
    scale_h = want_h if (want_h and (not src_h or want_h < src_h)) else 0

    if scale_h > 0:
        vf.append(f"scale=-2:{scale_h}:flags=bicubic")

    # 2) LUT
    lut = opts.get("lut")
    if lut:
        interp = opts.get("lut_interp", "tetrahedral")
        vf.append(f"lut3d=file='{lut}':interp={interp}")

    # 3) 亮度/饱和微调（LUT 之后再动手动参数）
    eq = []
    if abs(opts.get("brightness", 0.0)) > 1e-6:
        eq.append(f"brightness={opts['brightness']:.4f}")
    if abs(opts.get("contrast", 1.0) - 1.0) > 1e-6:
        eq.append(f"contrast={opts['contrast']:.4f}")
    if abs(opts.get("saturation", 1.0) - 1.0) > 1e-6:
        eq.append(f"saturation={opts['saturation']:.4f}")
    if eq:
        vf.append("eq=" + ":".join(eq))

    cmd = [str(FFMPEG), "-hide_banner", "-nostdin", "-y"]

    # 硬件解码：对 4K HEVC 10bit 提速极大（实测约 7 倍）
    if opts.get("hw_decode", True):
        cmd += ["-hwaccel", "videotoolbox"]

    # 多线程滤镜：lut3d 是主要瓶颈，这个选项很关键
    cmd += ["-filter_threads", str(opts.get("filter_threads", 0) or os.cpu_count() or 4)]

    # 快速定位（留 2 帧精度）
    if opts.get("start"):
        cmd += ["-ss", str(opts["start"])]
    cmd += ["-i", str(src)]
    if opts.get("duration"):
        cmd += ["-t", str(opts["duration"])]

    # 映射：视频 + 音频
    cmd += ["-map", "0:v:0"]
    if not opts.get("no_audio"):
        cmd += ["-map", "0:a?"]
    # DJI 的 djmd 陀螺仪轨无法封装进 MP4（MOV 可以）。
    # 保留时自动改用 .mov 容器，由调用方保证扩展名。
    if opts.get("keep_data"):
        cmd += ["-map", "0:d?", "-c:d", "copy"]
    cmd += ["-map_metadata", "0"]
    cmd += ["-map_chapters", "0"]

    if vf:
        cmd += ["-vf", ",".join(vf)]

    # 编码器
    enc = opts.get("encoder", "videotoolbox")
    quality = opts.get("quality", "high")
    if enc == "videotoolbox":
        cmd += ["-c:v", "hevc_videotoolbox"]
        # 硬件编码用码率控制，按实际输出高度选档
        eff = scale_h or src_h or 2160
        if eff <= 1080:
            br = {"high": 22, "medium": 14, "small": 8}.get(quality, 22)
        elif eff <= 1440:
            br = {"high": 35, "medium": 22, "small": 12}.get(quality, 35)
        else:
            br = {"high": 55, "medium": 30, "small": 15}.get(quality, 55)
        cmd += ["-b:v", f"{br}M", "-maxrate", f"{int(br*1.5)}M", "-bufsize", f"{br*2}M"]
        cmd += ["-profile:v", "main10", "-pix_fmt", "p010le"]
        cmd += ["-tag:v", "hvc1"]
    else:  # libx265
        cmd += ["-c:v", "libx265"]
        crf = {"high": 18, "medium": 21, "small": 25}.get(quality, 18)
        cmd += ["-crf", str(crf), "-preset", opts.get("preset", "fast")]
        cmd += ["-pix_fmt", "yuv420p10le" if opts.get("ten_bit", True) else "yuv420p"]
        cmd += ["-tag:v", "hvc1"]

    # 音频
    if not opts.get("no_audio"):
        cmd += ["-c:a", "aac", "-b:a", "192k"]

    # 输出色彩标记：LUT 输出的是 Rec.709
    cmd += [
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
    ]
    cmd += ["-movflags", "+faststart"]
    cmd += [str(dst)]
    return cmd


def out_name(src: Path, opts: dict) -> str:
    """输出文件名。保留陀螺仪数据轨时必须用 .mov 容器。"""
    ext = ".mov" if opts.get("keep_data") else ".mp4"
    return src.stem + "_Rec709" + ext


def transcode(src: Path, dst: Path, opts: dict, on_progress=None, cancel=None) -> tuple:
    """执行单个转码。返回 (成功?, 错误信息)。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    total = info.get("dur") or 0
    # 把源高度交给 build_cmd，用于避免放大
    opts = dict(opts)
    opts["_src_h"] = info.get("h") or 0

    cmd = build_cmd(src, dst, opts)
    # 用 -progress 读进度
    cmd = cmd[:1] + ["-progress", "pipe:1", "-loglevel", "error"] + cmd[1:]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )

    err_lines = []

    def drain_err():
        for line in proc.stderr:
            err_lines.append(line.rstrip())

    t = threading.Thread(target=drain_err, daemon=True)
    t.start()

    last = 0.0
    try:
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                break
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = float(line.split("=", 1)[1])
                    cur = us / 1_000_000.0
                    # out_time 有时会超出，做个上限保护
                    if total > 0 and 0 <= cur:
                        pct = min(99.0, cur / total * 100.0)
                        if on_progress and pct - last >= 1.0:
                            last = pct
                            on_progress(pct, cur, total)
                except Exception:
                    pass
            elif line.startswith("progress=") and line.endswith("end"):
                if on_progress:
                    on_progress(100.0, total, total)
    finally:
        proc.wait()
        t.join(timeout=2)

    if cancel is not None and cancel.is_set():
        try:
            if dst.exists():
                dst.unlink()
        except Exception:
            pass
        return False, "已取消"

    if proc.returncode != 0:
        msg = "\n".join(err_lines[-8:]) or f"ffmpeg 退出码 {proc.returncode}"
        # 失败时删掉半成品，避免留下坏文件
        try:
            if dst.exists() and dst.stat().st_size == 0:
                dst.unlink()
        except Exception:
            pass
        return False, msg

    if not dst.exists() or dst.stat().st_size == 0:
        return False, "输出文件为空"
    return True, ""


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def cli(argv) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="djilogfix",
        description="DJI Log 素材批量还原工具（只读源文件，输出到新目录）",
    )
    ap.add_argument("inputs", nargs="*", help="源视频文件或目录")
    ap.add_argument("-o", "--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("-l", "--lut", help="LUT 的 .cube 路径，或用 --list-luts 查看内置")
    ap.add_argument("--list-luts", action="store_true", help="列出内置 LUT 后退出")
    ap.add_argument("--height", type=int, default=2160, help="输出高度（2160/1440/1080），0 保持原样")
    ap.add_argument("--encoder", choices=["videotoolbox", "x265"], default="videotoolbox")
    ap.add_argument("--quality", choices=["high", "medium", "small"], default="high")
    ap.add_argument("--brightness", type=float, default=0.0)
    ap.add_argument("--contrast", type=float, default=1.0)
    ap.add_argument("--saturation", type=float, default=1.0)
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--keep-data", action="store_true",
                    help="保留 DJI 陀螺仪数据轨（会输出 .mov 容器）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    ap.add_argument("--overwrite", action="store_true", help="允许覆盖输出目录中的同名文件")
    args = ap.parse_args(argv)

    if args.list_luts:
        for name, path, cat in list_luts():
            print(f"[{cat}] {name}\n    {path}")
        return 0

    if not args.inputs:
        ap.print_help()
        return 1

    files = collect_files(args.inputs)
    if not files:
        print("没有找到视频文件", file=sys.stderr)
        return 1

    lut = args.lut
    if not lut:
        builtin = list_luts()
        if builtin:
            lut = str(builtin[0][1])
            print(f"使用内置 LUT: {builtin[0][0]}")

    opts = {
        "lut": lut,
        "height": args.height,
        "encoder": args.encoder,
        "quality": args.quality,
        "brightness": args.brightness,
        "contrast": args.contrast,
        "saturation": args.saturation,
        "no_audio": args.no_audio,
        "keep_data": args.keep_data,
    }

    outdir = Path(args.out).expanduser()
    print(f"源文件 {len(files)} 个 → 输出到 {outdir}\n")

    ok = fail = skip = 0
    for i, src in enumerate(files, 1):
        dst = outdir / out_name(src, opts)
        if dst.exists() and not args.overwrite:
            print(f"[{i}/{len(files)}] 跳过（已存在）: {dst.name}")
            skip += 1
            continue
        if args.dry_run:
            o = dict(opts)
            o["_src_h"] = (probe(src).get("h") or 0)
            print("  " + " ".join(build_cmd(src, dst, o)))
            continue

        print(f"[{i}/{len(files)}] {src.name}")
        lastpct = [-1]

        def prog(pct, cur, total):
            p = int(pct)
            if p != lastpct[0]:
                lastpct[0] = p
                sys.stdout.write(f"\r    {p:3d}%  {fmt_dur(cur)}/{fmt_dur(total)}")
                sys.stdout.flush()

        t0 = time.time()
        good, err = transcode(src, dst, opts, on_progress=prog)
        sys.stdout.write("\r" + " " * 50 + "\r")
        if good:
            el = time.time() - t0
            print(f"    完成 → {dst.name}  ({fmt_size(dst.stat().st_size)}, {el:.0f}s)")
            ok += 1
        else:
            print(f"    失败: {err[:200]}", file=sys.stderr)
            fail += 1

    print(f"\n完成 {ok} 个，跳过 {skip} 个，失败 {fail} 个")
    return 0 if fail == 0 else 1


def collect_files(inputs) -> list:
    """把输入展开成视频文件列表。"""
    out = []
    for item in inputs:
        p = Path(item).expanduser()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.suffix.lower() in VIDEO_EXT and not f.name.startswith("._"):
                    out.append(f)
        elif p.is_file() and p.suffix.lower() in VIDEO_EXT:
            out.append(p)
    # 去重保序
    seen, uniq = set(), []
    for f in out:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(f)
    return uniq


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------

def gui() -> int:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("DJI LogFix —— DJI Log 素材批量还原")
    root.geometry("980x720")
    root.minsize(880, 640)

    state = {
        "files": [],          # [(Path, info_dict)]
        "running": False,
        "cancel": threading.Event(),
        "worker": None,
    }

    # ---------------- 顶部：文件选择 ----------------
    top = ttk.LabelFrame(root, text=" 1. 选择素材（只读，不会改动原文件） ", padding=10)
    top.pack(fill="x", padx=12, pady=(12, 6))

    btnrow = ttk.Frame(top)
    btnrow.pack(fill="x")

    def add_paths(paths):
        added = 0
        for p in paths:
            p = Path(p)
            if p.is_dir():
                cand = collect_files([p])
            else:
                cand = [p] if p.suffix.lower() in VIDEO_EXT else []
            for f in cand:
                if any(f == x[0] for x in state["files"]):
                    continue
                inf = probe(f)
                if not inf:
                    continue
                state["files"].append((f, inf))
                added += 1
        refresh_list()
        if added:
            set_status(f"已添加 {added} 个文件，共 {len(state['files'])} 个")

    def choose_files():
        paths = filedialog.askopenfilenames(
            title="选择 DJI Log 视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.m4v *.mts"), ("所有文件", "*.*")],
        )
        if paths:
            add_paths(paths)

    def choose_dir():
        d = filedialog.askdirectory(title="选择素材文件夹（会递归查找视频）")
        if d:
            add_paths([d])

    def clear_all():
        state["files"].clear()
        refresh_list()
        set_status("已清空列表")

    ttk.Button(btnrow, text="选择文件…", command=choose_files, width=14).pack(side="left")
    ttk.Button(btnrow, text="选择文件夹…", command=choose_dir, width=14).pack(side="left", padx=6)
    ttk.Button(btnrow, text="清空", command=clear_all, width=8).pack(side="left")

    hint = ttk.Label(
        btnrow,
        text="提示：Osmo Action / Pocket 素材常见为 4K50 10bit，文件较大",
        foreground="#666",
    )
    hint.pack(side="right")

    # ---------------- 文件列表 ----------------
    mid = ttk.LabelFrame(root, text=" 2. 待处理列表 ", padding=6)
    mid.pack(fill="both", expand=True, padx=12, pady=6)

    cols = ("name", "res", "fps", "dur", "size", "codec")
    tv = ttk.Treeview(mid, columns=cols, show="headings", height=8, selectmode="extended")
    for c, txt, w, anchor in [
        ("name", "文件名", 380, "w"),
        ("res", "分辨率", 100, "center"),
        ("fps", "帧率", 70, "center"),
        ("dur", "时长", 90, "center"),
        ("size", "大小", 100, "e"),
        ("codec", "编码", 130, "center"),
    ]:
        tv.heading(c, text=txt)
        tv.column(c, width=w, anchor=anchor)
    tv.pack(side="left", fill="both", expand=True)

    sb = ttk.Scrollbar(mid, orient="vertical", command=tv.yview)
    sb.pack(side="right", fill="y")
    tv.configure(yscrollcommand=sb.set)

    def refresh_list():
        tv.delete(*tv.get_children())
        for f, inf in state["files"]:
            tv.insert(
                "", "end",
                values=(
                    f.name,
                    f"{inf['w']}×{inf['h']}" if inf["w"] else "?",
                    f"{inf['fps']:.0f}p" if inf["fps"] else "?",
                    fmt_dur(inf["dur"]),
                    fmt_size(inf["size"]),
                    f"{inf['codec']} {inf['pix_fmt']}",
                ),
            )
        update_summary()

    def remove_selected():
        sel = set(tv.selection())
        if not sel:
            return
        idxs = sorted((tv.index(i) for i in sel), reverse=True)
        for i in idxs:
            if 0 <= i < len(state["files"]):
                state["files"].pop(i)
        refresh_list()
        set_status("已移除所选")

    ttk.Button(btnrow, text="移除所选", command=remove_selected, width=10).pack(side="left", padx=6)

    # ---------------- 参数 ----------------
    opt = ttk.LabelFrame(root, text=" 3. 处理参数 ", padding=10)
    opt.pack(fill="x", padx=12, pady=6)

    luts = list_luts()
    lut_names = [f"[{c}] {n}" for n, p, c in luts]

    r1 = ttk.Frame(opt); r1.pack(fill="x", pady=3)
    ttk.Label(r1, text="LUT：", width=12).pack(side="left")
    lut_var = tk.StringVar(value=lut_names[0] if lut_names else "")
    lut_cb = ttk.Combobox(r1, textvariable=lut_var, values=lut_names, state="readonly", width=52)
    lut_cb.pack(side="left")

    def browse_lut():
        f = filedialog.askopenfilename(title="选择 .cube LUT", filetypes=[("Cube LUT", "*.cube")])
        if f:
            lut_var.set(f"（外部）{Path(f).name}")
            lut_cb.configure(values=lut_names + [f"（外部）{Path(f).name}"])
            state["ext_lut"] = f

    ttk.Button(r1, text="浏览…", command=browse_lut, width=8).pack(side="left", padx=6)
    ttk.Label(
        r1, text="官方 D-Log M→Rec.709 用于还原；「Rec.709 风格化」只能用在已还原素材上",
        foreground="#888",
    ).pack(side="left", padx=6)

    r2 = ttk.Frame(opt); r2.pack(fill="x", pady=3)
    ttk.Label(r2, text="输出分辨率：", width=12).pack(side="left")
    res_var = tk.StringVar(value="2160")
    for label, val in [("4K (2160p)", "2160"), ("2.7K (1440p)", "1440"),
                       ("1080p（快 ~4 倍）", "1080"), ("保持原样", "0")]:
        ttk.Radiobutton(r2, text=label, variable=res_var, value=val).pack(side="left", padx=4)

    r3 = ttk.Frame(opt); r3.pack(fill="x", pady=3)
    ttk.Label(r3, text="编码质量：", width=12).pack(side="left")
    q_var = tk.StringVar(value="high")
    for label, val in [("高（码率高，接近原片）", "high"),
                       ("中", "medium"), ("小（省空间）", "small")]:
        ttk.Radiobutton(r3, text=label, variable=q_var, value=val).pack(side="left", padx=4)
    ttk.Label(r3, text="   编码器：", width=9).pack(side="left")
    enc_var = tk.StringVar(value="videotoolbox")
    ttk.Radiobutton(r3, text="硬件（快）", variable=enc_var, value="videotoolbox").pack(side="left", padx=4)
    ttk.Radiobutton(r3, text="x265（更小）", variable=enc_var, value="x265").pack(side="left", padx=4)

    r4 = ttk.Frame(opt); r4.pack(fill="x", pady=3)
    ttk.Label(r4, text="色彩微调：", width=12).pack(side="left")
    bri_var = tk.DoubleVar(value=0.0)
    con_var = tk.DoubleVar(value=1.0)
    sat_var = tk.DoubleVar(value=1.0)
    for label, var, lo, hi in [("亮度", bri_var, -0.3, 0.3), ("对比", con_var, 0.5, 1.8), ("饱和", sat_var, 0.3, 2.0)]:
        ttk.Label(r4, text=f"{label}").pack(side="left", padx=(10, 2))
        ttk.Scale(r4, from_=lo, to=hi, variable=var, length=120).pack(side="left")
        ttk.Label(r4, textvariable=var, width=6).pack(side="left")

    r5 = ttk.Frame(opt); r5.pack(fill="x", pady=3)
    out_var = tk.StringVar(value=str(DEFAULT_OUT))
    ttk.Label(r5, text="输出目录：", width=12).pack(side="left")
    ttk.Entry(r5, textvariable=out_var, width=60).pack(side="left")
    ttk.Button(
        r5, text="更改…", width=8,
        command=lambda: out_var.set(filedialog.askdirectory(title="选择输出目录") or out_var.get()),
    ).pack(side="left", padx=6)

    r6 = ttk.Frame(opt); r6.pack(fill="x", pady=3)
    ow_var = tk.BooleanVar(value=False)
    au_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(r6, text="覆盖已存在的输出文件", variable=ow_var).pack(side="left", padx=(0, 12))
    ttk.Checkbutton(r6, text="不保留音频", variable=au_var).pack(side="left")

    # ---------------- 底部：执行 ----------------
    bot = ttk.Frame(root, padding=(12, 6))
    bot.pack(fill="x")

    run_btn = ttk.Button(bot, text="开始处理", width=16)
    run_btn.pack(side="left")
    stop_btn = ttk.Button(bot, text="停止", width=10, state="disabled")
    stop_btn.pack(side="left", padx=6)
    prev_btn = ttk.Button(bot, text="预览一帧", width=12)
    prev_btn.pack(side="left", padx=6)

    summary = ttk.Label(bot, text="", foreground="#333")
    summary.pack(side="right")

    # ---------------- 进度 ----------------
    pf = ttk.Frame(root, padding=(12, 0))
    pf.pack(fill="x")
    pbar = ttk.Progressbar(pf, mode="determinate", maximum=100)
    pbar.pack(fill="x")
    status = ttk.Label(root, text="就绪", anchor="w", foreground="#444", padding=(14, 4))
    status.pack(fill="x")
    logbox = tk.Text(root, height=7, wrap="none", background="#f7f7f7", foreground="#222")
    logbox.pack(fill="both", expand=False, padx=12, pady=(0, 12))
    logbox.configure(state="disabled")

    def log(msg):
        logbox.configure(state="normal")
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    def set_status(msg):
        status.configure(text=msg)

    def update_summary():
        n = len(state["files"])
        total = sum(i["size"] for _, i in state["files"])
        dur = sum(i["dur"] for _, i in state["files"])
        summary.configure(
            text=f"共 {n} 个 / {fmt_size(total)} / {fmt_dur(dur)}" if n else "未选择素材"
        )

    def current_lut_path():
        name = lut_var.get()
        ext = state.get("ext_lut")
        if ext and name.startswith("（外部）"):
            return ext
        for n, p, c in luts:
            if f"[{c}] {n}" == name:
                return str(p)
        return str(luts[0][1]) if luts else None

    def gather_opts():
        return {
            "lut": current_lut_path(),
            "height": int(res_var.get()),
            "quality": q_var.get(),
            "encoder": enc_var.get(),
            "brightness": bri_var.get(),
            "contrast": con_var.get(),
            "saturation": sat_var.get(),
            "no_audio": au_var.get(),
            "filter_threads": os.cpu_count() or 4,
        }

    # ---------------- 预览 ----------------
    def preview():
        if not state["files"]:
            messagebox.showinfo("提示", "请先选择素材")
            return
        src, inf = state["files"][tv.index(tv.selection()[0]) if tv.selection() else 0]
        opts = gather_opts()
        t = min(inf["dur"] * 0.3, 30) if inf["dur"] else 1
        out = APP_DIR / "预览.png"
        cmd = [str(FFMPEG), "-hide_banner", "-v", "error", "-y"]
        if inf["w"] >= 3000:
            cmd += ["-hwaccel", "videotoolbox"]
        cmd += ["-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1"]
        vf = []
        if opts["height"]:
            vf.append(f"scale=-2:{min(opts['height'], 1080)}")
        if opts["lut"]:
            vf.append(f"lut3d=file='{opts['lut']}'")
        eq = []
        if abs(opts["brightness"]) > 1e-6:
            eq.append(f"brightness={opts['brightness']:.4f}")
        if abs(opts["contrast"] - 1.0) > 1e-6:
            eq.append(f"contrast={opts['contrast']:.4f}")
        if abs(opts["saturation"] - 1.0) > 1e-6:
            eq.append(f"saturation={opts['saturation']:.4f}")
        if eq:
            vf.append("eq=" + ":".join(eq))
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += [str(out)]

        set_status("正在生成预览…")
        root.update_idletasks()
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and out.exists():
            set_status(f"预览已保存：{out}")
            log(f"预览帧已生成：{out}")
            subprocess.run(["open", str(out)], capture_output=True)
        else:
            set_status("预览失败")
            log("预览失败: " + (r.stderr or "")[:300])

    # ---------------- 执行 ----------------
    def worker():
        files = list(state["files"])
        opts = gather_opts()
        outdir = Path(out_var.get()).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)

        # 磁盘空间预检（按源体积 1.1 倍估算）
        need = sum(i["size"] for _, i in files) * 1.1
        try:
            free = shutil.disk_usage(outdir).free
            if free < need:
                log(f"⚠ 输出盘可用 {fmt_size(free)}，估计需要约 {fmt_size(need)}，可能空间不足")
        except Exception:
            pass

        ok = fail = skip = 0
        t_all = time.time()
        for idx, (src, inf) in enumerate(files, 1):
            if state["cancel"].is_set():
                log("已停止")
                break
            dst = outdir / (src.stem + "_Rec709.mp4")
            if dst.exists() and not ow_var.get():
                log(f"[{idx}/{len(files)}] 跳过（已存在）: {dst.name}")
                skip += 1
                continue
            log(f"[{idx}/{len(files)}] {src.name}  ({fmt_dur(inf['dur'])}, {fmt_size(inf['size'])})")
            set_status(f"处理中 {idx}/{len(files)}：{src.name}")
            t0 = time.time()
            lastpct = [-1]

            def prog(pct, cur, total):
                p = int(pct)
                if p != lastpct[0]:
                    lastpct[0] = p
                    root.after(0, lambda: pbar.configure(value=p))
                    root.after(
                        0, lambda: set_status(
                            f"处理中 {idx}/{len(files)}：{src.name}  {p}%  "
                            f"{fmt_dur(cur)}/{fmt_dur(total)}"
                        )
                    )

            good, err = transcode(src, dst, opts, on_progress=prog, cancel=state["cancel"])
            if good:
                el = time.time() - t0
                speed = (inf["dur"] / el) if el > 0 else 0
                log(f"    完成 → {dst.name}  ({fmt_size(dst.stat().st_size)}, "
                    f"耗时 {el:.0f}s, {speed:.2f}x)")
                ok += 1
            else:
                log(f"    失败: {err[:300]}")
                fail += 1
            root.after(0, lambda: pbar.configure(value=0))

        total_el = time.time() - t_all
        log(f"\n=== 全部结束：成功 {ok}，跳过 {skip}，失败 {fail}，总耗时 {total_el:.0f}s ===")
        log(f"输出目录：{outdir}")
        root.after(0, lambda: pbar.configure(value=100 if ok else 0))
        root.after(0, lambda: set_status(f"完成：成功 {ok}，跳过 {skip}，失败 {fail}"))
        root.after(0, lambda: run_btn.configure(state="normal"))
        root.after(0, lambda: stop_btn.configure(state="disabled"))
        root.after(0, lambda: prev_btn.configure(state="normal"))
        state["running"] = False

    def start():
        if state["running"]:
            return
        if not state["files"]:
            messagebox.showinfo("提示", "请先选择素材")
            return
        if not current_lut_path():
            if not messagebox.askyesno("确认", "没有选择 LUT，将只做转码（不还原色彩）。继续？"):
                return
        # 检查输出目录是否会与源目录重叠造成误覆盖
        outdir = Path(out_var.get()).expanduser().resolve()
        for src, _ in state["files"]:
            if src.resolve().parent == outdir:
                if not messagebox.askyesno(
                    "注意",
                    "输出目录与部分源文件所在目录相同，\n"
                    "输出文件会带 _Rec709 后缀，不会覆盖原片。继续？",
                ):
                    return
                break

        state["running"] = True
        state["cancel"].clear()
        run_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        prev_btn.configure(state="disabled")
        pbar.configure(value=0)
        logbox.configure(state="normal"); logbox.delete("1.0", "end"); logbox.configure(state="disabled")
        threading.Thread(target=worker, daemon=True).start()

    def stop():
        state["cancel"].set()
        set_status("正在停止…")
        log("已请求停止，等待当前文件结束…")

    run_btn.configure(command=start)
    stop_btn.configure(command=stop)
    prev_btn.configure(command=preview)

    root.mainloop()
    return 0


# --------------------------------------------------------------------------

def main():
    if not FFMPEG.exists():
        print(f"错误：找不到 ffmpeg：{FFMPEG}", file=sys.stderr)
        return 2
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        return cli(sys.argv[2:])
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    return gui()


if __name__ == "__main__":
    sys.exit(main())
