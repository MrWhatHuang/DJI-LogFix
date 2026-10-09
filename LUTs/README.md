# LUTs

本目录下的 `.cube` 文件**版权归 DJI 大疆所有**，因此不纳入版本控制。

请运行以下命令从 DJI 官方 CDN 获取：

```bash
python3 tools/get_luts.py
```

或直接使用网页界面里的「**下载官方 LUT**」按钮。

## 获取后应得到

```
LUTs/
├── 官方D-LogM/
│   └── Action5Pro_D-LogM_to_Rec709_V1.cube    ← 还原用（必需）
└── 官方风格化/
    ├── DJI OSMO Action 5 Pro Mei.cube
    ├── DJI OSMO Action 5 Pro Ju.cube
    ├── DJI OSMO Action 5 Pro Zhu.cube
    └── DJI OSMO Action 5 Pro Lan.cube         ← 风格化用（可选）
```

也可以手动从 DJI 下载中心获取：
<https://www.dji.com/cn/downloads/products/osmo-action-5-pro>

搜索「OSMO Action 5 Pro D-Log M to Rec.709 LUT」。
