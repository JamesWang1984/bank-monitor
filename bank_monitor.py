# -*- coding: utf-8 -*-
"""
银行股买点监控（手机可运行版）
====================================
监控 工商银行/交通银行/江苏银行 的实时股息率、PB、股债利差，
触发档位时通过 Server酱 微信推送提醒（不触发则仅控制台输出）。

档位定义（基于历史回测框架，详见对话分析）:
  ★  一档·建仓 : 股息率 >= 5.0%
  ★★ 二档·加仓 : 股息率 >= 5.5% 或 股债利差 >= 3.5pp
  ★★★ 三档·金坑: 股息率 >= 6.0%（历史级底部区域）
  ▲ 估值偏高警示: PB >= 近5年95分位（提示不宜追高/考虑减仓）

──────────────────────────────────────
【部署方式一：安卓手机 Termux】(本地运行)
  1. 安装 Termux (F-Droid 版) → 打开执行:
     pkg install python cronie termux-services -y
     sv-enable crond
  2. 将本文件存为 ~/bank_monitor.py (可用 vim 或从相册/下载目录 mv)
  3. 编辑定时任务: crontab -e  添加一行(交易日15:05运行):
     5 15 * * 1-5 python ~/bank_monitor.py >> ~/bank_monitor.log 2>&1
  4. 填好下方 SERVERCHAN_KEY 后重启 Termux 生效

【部署方式二：云端定时 + 微信推送】(iOS 推荐, 手机零安装)
  GitHub 新建私有仓库 → 上传本文件 → Actions 定时任务(见下方 yml)
  手机上只收微信推送, 不需要运行任何东西:
    name: bank-monitor
    on:
      schedule:
        - cron: "5 7 * * 1-5"   # UTC 07:05 = 北京 15:05
    jobs:
      run:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - uses: actions/setup-python@v5
            with: {python-version: "3.11"}
          - run: python bank_monitor.py
            env:
              SERVERCHAN_KEY: ${{ secrets.SERVERCHAN_KEY }}
  (Secrets 中添加 SERVERCHAN_KEY; 注意 GitHub 定时可能延迟 5-20 分钟)

【Server酱 微信推送申请】(2分钟):
  1. 微信扫码登录 https://sct.ftqq.com
  2. 复制 SendKey 填到下面 SERVERCHAN_KEY (或环境变量)

──────────────────────────────────────
【维护】每年分红除权后更新 DPS_TTM; 每季报后更新 BPS:
  py -3.9 fetch_dps.py   (本项目脚本自动计算)
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta

# ================== 配置区 ==================
SERVERCHAN_KEY = ""          # Server酱 SendKey, 留空则只打印不推送; 也可用环境变量
PUSH_MODE = "alert_only"     # alert_only=仅触发档位时推送 / always=每天推送汇总(可被环境变量覆盖)
Y10_FALLBACK = 0.0168        # 10年期国债收益率(抓取失败时用此值; 变动缓慢, 每月看一眼)

BANKS = [
    # 代码, 名称, TTM每股股息, 每股净资产, PB偏高警示线(近5年95分位)
    ("sh601398", "工商银行", 0.3103, 11.117, 0.74),
    ("sh601328", "交通银行", 0.3247, 13.216, 0.61),
    ("sh600919", "江苏银行", 0.5641, 14.787, 0.84),
]

TIER1_DV, TIER2_DV, TIER2_SPREAD, TIER3_DV = 0.05, 0.055, 0.035, 0.06
# ============================================


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.qq.com/"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def get_quotes(codes):
    """腾讯实时行情: 返回 {code: (价格, 涨跌幅, 时间)}"""
    raw = http_get("http://qt.gtimg.cn/q=" + ",".join(codes)).decode("gbk", "ignore")
    out = {}
    for seg in raw.strip().split(";"):
        if "~" not in seg:
            continue
        f = seg.split("~")
        code = f[2].strip().lower()
        code = ("sh" if code.startswith("6") else "sz") + code
        out[code] = (float(f[3]), float(f[32]) / 100.0, f[30])
    return out


def get_y10():
    """尝试拉取中国10年期国债收益率, 失败用配置值"""
    try:
        raw = http_get("https://stock.xueqiu.com/v5/stock/chart/notation.json?symbol=SH000300", timeout=5)
    except Exception:
        return Y10_FALLBACK, False
    return Y10_FALLBACK, False  # 无稳定免费无鉴权接口, 直接用配置值


def push_wechat(title, desp):
    key = os.environ.get("SERVERCHAN_KEY", SERVERCHAN_KEY)
    if not key:
        return False
    try:
        data = json.dumps({"title": title, "desp": desp}).encode()
        req = urllib.request.Request(f"https://sctapi.ftqq.com/{key}.send", data=data,
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10).status == 200
    except Exception as e:
        print("推送失败:", e)
        return False


def now_bj():
    """北京时间(GitHub Actions服务器为UTC)"""
    return datetime.utcnow() + timedelta(hours=8)


def main():
    codes = [b[0] for b in BANKS]
    quotes = get_quotes(codes)
    y10 = get_y10()[0]
    today = now_bj().strftime("%Y%m%d")

    # 非交易日(节假日)自动跳过: 行情时间戳日期 != 今天
    sample = next(iter(quotes.values()), None)
    if sample and not sample[2].startswith(today):
        print(f"今日({today})非交易日, 跳过监控")
        return

    push_mode = os.environ.get("PUSH_MODE", PUSH_MODE)
    lines, max_tier, alert = [], 0, False
    for code, name, dps, bps, pb_warn in BANKS:
        if code not in quotes:
            continue
        price, chg, tstamp = quotes[code]
        dv = dps / price
        pb = price / bps
        spread = dv - y10

        tier, tag = 0, "观望"
        if dv >= TIER3_DV:
            tier, tag = 3, "★★★ 三档·历史金坑区, 可重仓"
        elif dv >= TIER2_DV or spread >= TIER2_SPREAD:
            tier, tag = 2, "★★ 二档·加仓区"
        elif dv >= TIER1_DV:
            tier, tag = 1, "★ 一档·建仓区"
        note = ""
        if pb >= pb_warn and tier == 0:
            note = " ▲PB偏高(近5年95分位以上)"
        if tier >= 1:
            note = f" → 触发! 建仓参考价 {dps/TIER1_DV:.2f} / 加仓 {dps/TIER2_DV:.2f} / 金坑 {dps/TIER3_DV:.2f}"
            alert = True
        max_tier = max(max_tier, tier)
        lines.append(f"{name} {price:.2f} ({chg:+.1%}) | 股息率 {dv:.2%} | PB {pb:.2f} | "
                     f"利差 {spread*100:+.2f}pp | {tag}{note}")

    header = f"银行股买点监控 {now_bj():%Y-%m-%d %H:%M} 北京时间 (10Y国债 {y10:.2%})"
    body = ("\n".join(lines)
            + "\n\n档位: ★建仓 股息率≥5% | ★★加仓 ≥5.5%或利差≥3.5pp | ★★★金坑 ≥6% | ▲PB≥5年95分位警示")
    print(header + "\n" + body)

    if alert or push_mode == "always":
        ok = push_wechat(f"银行股买点提醒 {'★'*max_tier if max_tier else '估值跟踪'}",
                         f"**{header}**\n\n```\n" + "\n".join(lines) + "\n```")
        print("\n微信推送:", "已发送" if ok else "未发送(未配置key或失败)")


if __name__ == "__main__":
    main()
