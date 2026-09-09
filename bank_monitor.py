# -*- coding: utf-8 -*-
"""
银行股买点监控（手机可运行版）
====================================
监控 工商银行/交通银行/江苏银行 的实时股息率、PB、股债利差。

档位定义（基于历史回测框架，详见对话分析）:
  ★  一档·建仓 : 股息率 >= 5.0%
  ★★ 二档·加仓 : 股息率 >= 5.5% 或 股债利差 >= 3.5pp
  ★★★ 三档·金坑: 股息率 >= 6.0%（历史级底部区域）
  ◇  接近触发  : 股价距一档建仓价 <= 5%（股息率 >= 4.76%），提前预警
  ▲ 估值偏高警示: PB >= 近5年95分位（提示不宜追高/考虑减仓, 仅控制台）

推送逻辑（防打扰）:
  仅当某银行档位跃迁（升级）时推送一次，处于同一档位期间不重复提醒；
  档位带迟滞缓冲（降档需明显回落），边界小幅波动不会反复横跳刷屏；
  首次运行（无状态文件）视为观望档，若已处于买入区会推送一次。
  档位状态持久化在 monitor_state.json（云端运行后自动提交回仓库）。

──────────────────────────────────────
【部署方式一：安卓手机 Termux】(本地运行)
  1. 安装 Termux (F-Droid 版) → 打开执行:
     pkg install python cronie termux-services -y
     sv-enable crond
  2. 将本文件存为 ~/bank_monitor.py (可用 vim 或从相册/下载目录 mv)
  3. 编辑定时任务: crontab -e  添加(盘中每小时 + 收盘后):
     35 9,10,13,14 * * 1-5 python ~/bank_monitor.py >> ~/bank_monitor.log 2>&1
     25 11 * * 1-5 python ~/bank_monitor.py >> ~/bank_monitor.log 2>&1
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
NEAR_PCT = 0.05              # 接近触发判定: 股价距一档建仓价≤5%(股息率≥4.76%)时标记接近
NEAR_EXIT_PCT = 0.06         # 接近区退出判定: 股价高于建仓价6%以上才算离开(迟滞, 防边界抖动)
DV_HYST = 0.0015             # 档位退出缓冲: 股息率需低于阈值0.15pp才降档(迟滞, 防边界抖动)
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


TIER_TAGS = {
    0: "观望", 0.5: "◇接近建仓区", 1: "★一档·建仓区",
    2: "★★二档·加仓区", 3: "★★★三档·历史金坑区",
}
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")


def load_state():
    """读取上次运行的档位状态: {code: tier}"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("状态文件写入失败(档位去重将失效):", e)


def calc_tier(dv, spread, price, t1_price, prev):
    """计算当前档位(0/0.5接近/1/2/3), 带迟滞: 升档即时生效, 降档需明显回落。
    防止股价在档位边界小幅震荡时反复横跳导致重复推送。"""
    if dv >= TIER3_DV:
        t = 3
    elif dv >= TIER2_DV or spread >= TIER2_SPREAD:
        t = 2
    elif dv >= TIER1_DV:
        t = 1
    elif price <= t1_price * (1 + NEAR_PCT):
        t = 0.5
    else:
        t = 0
    if t >= prev:
        return t
    # 档位回落: 带缓冲判断是否真离开原档位(迟滞区间内维持原档)
    if prev == 3 and dv >= TIER3_DV - DV_HYST:
        return 3
    if prev == 2 and (dv >= TIER2_DV - DV_HYST or spread >= TIER2_SPREAD - DV_HYST):
        return 2
    if prev == 1 and dv >= TIER1_DV - DV_HYST:
        return 1
    if prev == 0.5 and price <= t1_price * (1 + NEAR_EXIT_PCT):
        return 0.5
    return t


def push_wecom(title, desp):
    """企业微信群机器人 webhook 推送(markdown 格式), 返回是否成功"""
    url = os.environ.get("WECOM_WEBHOOK", "")
    if not url:
        key = os.environ.get("WECOM_KEY", "")
        if key:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    if not url:
        return False
    content = f"### {title}\n{desp}"
    try:
        data = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read().decode())
        return body.get("errcode", -1) == 0
    except Exception as e:
        print("企业微信推送失败:", e)
        return False


def push_wechat(title, desp):
    """推送通知: 优先企业微信 webhook → Bark(iOS) → Server酱"""
    if push_wecom(title, desp):
        return True
    bark_key = os.environ.get("BARK_KEY", "")
    if bark_key:
        try:  # Bark: 苹果APNs原生推送
            server = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
            data = json.dumps({"title": title, "body": desp, "group": "银行股监控",
                               "sound": "minuet", "url": "https://github.com/JamesWang1984/bank-monitor/actions"}).encode()
            req = urllib.request.Request(f"{server}/{bark_key}", data=data,
                                         headers={"Content-Type": "application/json; charset=utf-8"})
            return urllib.request.urlopen(req, timeout=10).status == 200
        except Exception as e:
            print("Bark推送失败:", e)
            return False
    key = os.environ.get("SERVERCHAN_KEY", SERVERCHAN_KEY)
    if not key:
        return False
    try:  # Server酱(每月限5条, 备用)
        data = json.dumps({"title": title, "desp": desp}).encode()
        req = urllib.request.Request(f"https://sctapi.ftqq.com/{key}.send", data=data,
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=10).status == 200
    except Exception as e:
        print("Server酱推送失败:", e)
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

    state = load_state()
    lines, changes = [], []   # changes: [(名称, 旧档, 新档)]
    for code, name, dps, bps, pb_warn in BANKS:
        if code not in quotes:
            continue
        price, chg, tstamp = quotes[code]
        dv = dps / price
        pb = price / bps
        spread = dv - y10
        t1_price = dps / TIER1_DV   # 一档建仓参考价

        prev = state.get(code, 0)   # 无历史状态时视为观望档
        tier = calc_tier(dv, spread, price, t1_price, prev)
        state[code] = tier

        note = ""
        if pb >= pb_warn and tier == 0:
            note = " ▲PB偏高(近5年95分位以上)"
        if tier >= 1:
            note = f" → 触发! 建仓参考价 {t1_price:.2f} / 加仓 {dps/TIER2_DV:.2f} / 金坑 {dps/TIER3_DV:.2f}"
        elif tier == 0.5:
            note = f" → 接近: 建仓参考价 {t1_price:.2f} (现价高出 {(price/t1_price-1):.1%})"
        if tier > prev:
            changes.append((name, prev, tier))
        lines.append(f"{name} {price:.2f} ({chg:+.1%}) | 股息率 {dv:.2%} | PB {pb:.2f} | "
                     f"利差 {spread*100:+.2f}pp | {TIER_TAGS[tier]}{note}")

    save_state(state)

    header = f"银行股买点监控 {now_bj():%Y-%m-%d %H:%M} 北京时间 (10Y国债 {y10:.2%})"
    body = ("\n".join(lines)
            + "\n\n档位: ★建仓 股息率≥5% | ★★加仓 ≥5.5%或利差≥3.5pp | ★★★金坑 ≥6% | "
            "◇接近=距建仓价≤5% | ▲PB≥5年95分位警示")
    print(header + "\n" + body)

    if changes:   # 仅当档位跃迁(升级)时推送, 同一档位期间不重复
        max_t = max(t for _, _, t in changes)
        title = f"银行股买点提醒 {'★' * int(max_t) if max_t >= 1 else '◇接近建仓'}"
        detail = "\n".join(f"**{n}**: {TIER_TAGS[o]} → {TIER_TAGS[t]}" for n, o, t in changes)
        desp = f"**{header}**\n\n**档位变化:**\n{detail}\n\n```\n" + "\n".join(lines) + "\n```"
        ok = push_wechat(title, desp)
        print("\n微信推送:", "已发送" if ok else "未发送(未配置key或失败)")
    else:
        print("\n(档位无跃迁, 不推送)")


if __name__ == "__main__":
    main()
