#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载 DJI 官方 LUT
=================

LUT 版权归 DJI 大疆所有，因此本仓库不直接附带，改为在此按需下载。

用法：
    python3 tools/get_luts.py
"""

import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
LUT_DIR = APP_DIR / "LUTs"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# DJI 官方 CDN 直链（2026-10 验证可用）
DJI_CDN = "https://terra-1-g.djicdn.com/851d20f7b9f64838a34cd02351370894"

ITEMS = [
    {
        "name": "D-Log M → Rec.709 还原 LUT（必需）",
        "url": f"{DJI_CDN}/AC204%E8%BD%AF%E4%BB%B6/"
               "DJI%20OSMO%20Action%205%20Pro%20D-Log%20M%20to%20Rec.709%20V1.cube",
        "dest": LUT_DIR / "官方D-LogM" / "Action5Pro_D-LogM_to_Rec709_V1.cube",
        "kind": "cube",
    },
    {
        "name": "Rec.709 风格化 LUT（梅/菊/竹/兰，可选）",
        "url": f"{DJI_CDN}/AC204%E8%BD%AF%E4%BB%B6/%E5%A4%A7%E5%B8%88%E6%BB%A4%E9%95%9C/"
               "DJI%20OSMO%20Action%205%20Pro%20Rec.709%20to%20Color%20Grading%20LUTs.zip",
        "dest": LUT_DIR / "官方风格化",
        "kind": "zip",
    },
]


def fetch(url: str, dest: Path) -> bool:
    """下载到 dest。返回是否成功。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            total = int(r.headers.get("Content-Length") or 0)
            data = r.read()
    except Exception as e:
        print(f"    下载失败: {e}")
        return False

    if total and len(data) != total:
        print(f"    下载不完整: {len(data)}/{total} 字节")
        return False

    dest.write_bytes(data)
    print(f"    已保存: {dest.name}  ({len(data)/1024/1024:.1f} MB)")
    return True


def main() -> int:
    print("=" * 62)
    print("  下载 DJI 官方 LUT")
    print("  LUT 版权归 DJI 大疆所有，本仓库不附带，改为按需获取")
    print("=" * 62)

    ok = skip = fail = 0
    for item in ITEMS:
        print(f"\n[{item['name']}]")

        dest = item["dest"]
        # 已存在就跳过（zip 类型看目录里有没有 cube）
        if item["kind"] == "cube" and dest.exists() and dest.stat().st_size > 0:
            print("    已存在，跳过")
            skip += 1
            continue
        if item["kind"] == "zip" and dest.is_dir() and list(dest.glob("*.cube")):
            print("    已存在，跳过")
            skip += 1
            continue

        if item["kind"] == "cube":
            if fetch(item["url"], dest):
                ok += 1
            else:
                fail += 1
        else:
            tmp = dest.parent / "_tmp_lut.zip"
            if not fetch(item["url"], tmp):
                fail += 1
                continue
            try:
                dest.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(tmp) as z:
                    n = 0
                    for m in z.namelist():
                        if m.lower().endswith(".cube"):
                            (dest / Path(m).name).write_bytes(z.read(m))
                            print(f"    解出: {Path(m).name}")
                            n += 1
                tmp.unlink()
                ok += 1 if n else 0
                fail += 0 if n else 1
            except Exception as e:
                print(f"    解压失败: {e}")
                fail += 1

    print("\n" + "=" * 62)
    print(f"完成：新增 {ok}，跳过 {skip}，失败 {fail}")
    if fail:
        print("\n若下载失败，可手动前往 DJI 下载中心搜索：")
        print("  「OSMO Action 5 Pro D-Log M to Rec.709 LUT」")
        print("  https://www.dji.com/cn/downloads/products/osmo-action-5-pro")
        print(f"\n下载后把 .cube 放到：{LUT_DIR / '官方D-LogM'}")
    else:
        print(f"LUT 已就绪：{LUT_DIR}")
    print("=" * 62)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
