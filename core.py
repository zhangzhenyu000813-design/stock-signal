# -*- coding: utf-8 -*-
"""招商证券策略提醒工具 · 核心逻辑（strategy.py 与网页服务共用）
策略 v2：趋势过滤 + 回调企稳买入 + 趋势破坏卖出 + 移动止盈"""
import urllib.request
import json
import datetime as dt
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

# ── 策略参数（可调） ──
TREND_WINDOW = 5       # MA20 斜率看最近几天
DRAWDOWN_MIN = 0.03    # 最小回调 3%
DRAWDOWN_MAX = 0.08    # 最大回调 8%
HARD_STOP = -0.08      # 硬止损 -8%
EXTREME_DROP = -0.05   # 单日跌幅 >5% 极端止损
TRAILING_STOP = 0.06   # 移动止盈回撤 6%


def _get(url, tries=2):
    """带重试的 HTTP GET，返回原始字节。超时 8s、重试 2 次，避免单只卡死。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            last = e
            time.sleep(1 * (i + 1))
    raise last


def sec_prefix(code: str):
    """股票代码 -> 交易所前缀 sh/sz/bj"""
    if code[:2] == "11" or code[0] in "6549":
        return "sh"
    if code[0] in "84":
        return "bj"
    return "sz"


def resolve_name(code: str):
    """通过腾讯实时接口取名称（GBK 编码）。返回 (sym, name)。"""
    sym = sec_prefix(code) + code
    try:
        raw = _get(f"https://qt.gtimg.cn/q={sym}")
        txt = raw.decode("gbk", "ignore")
        part = txt.split('="', 1)[1].rstrip('"')
        name = part.split("~")[1]
    except Exception:
        name = code
    return sym, name


def fetch_kline(sym: str, days: int = 260):
    """拉日K线。主用腾讯，兜底东方财富。返回 [{date,open,close,high,low,vol}]。"""
    # 主：腾讯
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
        j = json.loads(_get(url))
        node = j["data"][sym]
        kl = (node.get("qfqday") or node.get("day") or [])[-days:]
        return [{"date": p[0], "open": float(p[1]), "close": float(p[2]),
                 "high": float(p[3]), "low": float(p[4]), "vol": float(p[5])} for p in kl]
    except Exception:
        pass
    # 兜底：东方财富
    try:
        secid = ("1." if sym.startswith("sh") else "0.") + sym[2:]
        end = dt.date.today().strftime("%Y%m%d")
        beg = (dt.date.today() - dt.timedelta(days=days * 2)).strftime("%Y%m%d")
        url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
               f"&fields1=f1&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
               f"&klt=101&fqt=1&beg={beg}&end={end}&ut=fa5fd1943c7b386f172d6893dbfba10b")
        j = json.loads(_get(url))
        out = []
        for line in (j.get("data", {}) or {}).get("klines", []) or []:
            p = line.split(",")
            out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                        "high": float(p[3]), "low": float(p[4]), "vol": float(p[5])})
        return out[-days:]
    except Exception:
        return []


def ma(data, n):
    if len(data) < n:
        return None
    return sum(d["close"] for d in data[-n:]) / n


def rsi(data, n=14):
    if len(data) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        chg = data[i]["close"] - data[i - 1]["close"]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


# ── 策略 v2 辅助函数 ──

def ma_slope(data, n=20, window=TREND_WINDOW):
    """MA20 最近 window 天的斜率方向。返回 (斜率值, 方向标签)。
    正=向上，负=向下，0附近=走平。"""
    if len(data) < n + window:
        return 0.0, "走平"
    ma_now = ma(data[-n:], n)
    ma_before = ma(data[-(n + window):-window], n)
    if ma_now is None or ma_before is None:
        return 0.0, "走平"
    diff = ma_now - ma_before
    # 斜率相对于价格的比例，避免不同价位的 ETF 斜率口径不一致
    ratio = diff / ma_before if ma_before else 0
    if ratio > 0.005:   # >0.5% 判定向下
        return ratio, "向上"
    elif ratio < -0.005:
        return ratio, "向下"
    else:
        return ratio, "走平"


def drawdown_pct(data, lookback=20):
    """当前价相对近 lookback 天最高价的回撤幅度。返回比例（正数）。"""
    if len(data) < lookback:
        lookback = len(data)
    if lookback < 2:
        return 0.0
    high = max(d["high"] for d in data[-lookback:])
    price = data[-1]["close"]
    if high <= 0:
        return 0.0
    return (high - price) / high


def is_stable(data):
    """最近 2 天收盘价不创新低（企稳确认）。
    第-1天close >= 第-2天close >= 第-3天close → 连续2天止跌。"""
    if len(data) < 4:
        return False
    c1, c2, c3 = data[-1]["close"], data[-2]["close"], data[-3]["close"]
    return c1 >= c2 and c2 >= c3


def is_vol_shrink(data, short=5, long=20):
    """回调期间缩量：最近 5 天平均量 < 前 20 天平均量的 80%。"""
    if len(data) < long + short:
        return False
    recent_vol = sum(d["vol"] for d in data[-short:]) / short
    prev_vol = sum(d["vol"] for d in data[-(long + short):-short]) / long
    if prev_vol <= 0:
        return False
    return recent_vol < prev_vol * 0.8


def _analyze_one(code, per):
    """单只股票：拉数据 → 4层过滤算信号 → 返回一行。供线程池并发调用。
    买入条件：趋势向上 + 回调3-8% + 企稳 + (缩量或RSI合理)
    卖出条件（自选池）：趋势向下 + 死叉"""
    code = str(code).strip()
    sym, name = resolve_name(code)
    kl = fetch_kline(sym)
    if len(kl) < 26:  # 至少需要 20+5+1 天数据
        return {"code": code, "name": name, "action": "跳过",
                "reason": "数据不足", "price": None,
                "planned": "—", "stop": "",
                "trend": "—", "drawdown_pct": None,
                "stable": False, "vol_shrink": False,
                "signal_strength": "—", "rsi": None}

    price = kl[-1]["close"]
    m5, m20 = ma(kl, 5), ma(kl, 20)
    r = rsi(kl)
    slope_ratio, trend = ma_slope(kl)
    dd = drawdown_pct(kl)
    stable = is_stable(kl)
    vol_shrink = is_vol_shrink(kl)

    action = "持有/观望"
    reason = ""
    signal_strength = "—"

    # ── 卖出/减仓信号（自选池中的品种趋势转弱时提醒）──
    prev_m5 = ma(kl[:-1], 5)
    prev_m20 = ma(kl[:-1], 20)
    if prev_m5 and prev_m20 and m5 and m20:
        if prev_m5 >= prev_m20 and m5 < m20:
            action = "卖出/减仓"
            reason = f"MA5下穿MA20死叉，趋势转弱"
    if r and r >= 75 and action != "卖出/减仓":
        action = "卖出/减仓"
        reason = f"RSI {r:.0f} 超买"

    # ── 买入信号（4层过滤）──
    if action != "卖出/减仓":
        # 第1层：趋势方向过滤
        if trend != "向上":
            action = "持有/观望"
            reason = f"MA20趋势{trend}，不介入"
        # 第2层：回调深度判断
        elif dd < DRAWDOWN_MIN:
            action = "持有/观望"
            reason = f"回撤仅{dd*100:.1f}%，仍在高位待调"
        elif dd > DRAWDOWN_MAX:
            action = "持有/观望"
            reason = f"回撤{dd*100:.1f}%过大，可能趋势已破"
        # 第3层：企稳确认
        elif not stable:
            action = "持有/观望"
            reason = f"回调{dd*100:.1f}%但未企稳，不接飞刀"
        # 第4层：量能+RSI辅助
        else:
            rsi_ok = r is not None and 40 <= r <= 60
            if vol_shrink and rsi_ok:
                signal_strength = "强"
            else:
                signal_strength = "弱"
            parts = [f"趋势向上+回调{dd*100:.1f}%+企稳"]
            if vol_shrink:
                parts.append("缩量")
            if rsi_ok:
                parts.append(f"RSI{r:.0f}合理")
            action = "买入"
            reason = "+".join(parts)

    # ── 计划买入量 ──
    stop = round(price * (1 + HARD_STOP), 2)  # 止损价 = -8%
    shares = 0
    if action == "卖出/减仓":
        planned = "—（减仓）"
    elif action == "买入":
        lot_cost = price * 100
        if lot_cost <= per:
            shares = int(per // lot_cost) * 100
            if shares == 0:
                shares = 100
            planned = f"{shares}份({shares//100}手) / ¥{round(shares * price, 2)}"
        else:
            planned = "本金不足"
    else:
        planned = "—"

    return {"code": code, "name": name, "action": action,
            "reason": reason, "price": price, "planned": planned,
            "shares": shares, "stop": stop,
            "ma5": round(m5, 3) if m5 else None,
            "ma20": round(m20, 3) if m20 else None,
            "rsi": round(r, 1) if r else None,
            "trend": trend,
            "drawdown_pct": round(dd * 100, 1) if dd else 0,
            "stable": stable,
            "vol_shrink": vol_shrink,
            "signal_strength": signal_strength}


def compute_signals(codes, capital=1000):
    """对自选股并发算信号。返回 (rows, per, buys)。
    并发拉取，总耗时≈最慢一只，避免串行累加卡死。"""
    per = round(capital * 0.3, 2)
    clean = [str(c).strip() for c in codes if str(c).strip()]
    rows = []
    with ThreadPoolExecutor(max_workers=min(8, len(clean) or 1)) as ex:
        futs = {ex.submit(_analyze_one, c, per): c for c in clean}
        for f in as_completed(futs):
            try:
                rows.append(f.result(timeout=15))
            except Exception as e:
                rows.append({"code": futs[f], "name": futs[f], "action": "跳过",
                             "reason": f"超时/出错: {e}", "price": None,
                             "planned": "—", "stop": "",
                             "trend": "—", "drawdown_pct": None,
                             "stable": False, "vol_shrink": False,
                             "signal_strength": "—", "rsi": None})
    # 保持原始顺序
    order = {c: i for i, c in enumerate(clean)}
    rows.sort(key=lambda r: order.get(r["code"], 999))
    buys = [r for r in rows if r["action"] == "买入"]
    return rows, per, buys


def compute_holdings(portfolio):
    """对持仓簿算浮盈、更新最高价、检查卖出条件（趋势v2）。
    卖出优先级：1.硬止损-8%  2.单日跌>5%  3.趋势破坏(跌破MA20+MA20走平/向下)  4.移动止盈回撤6%
    返回 (alerts, portfolio)。alerts=需发邮件的卖出提醒；portfolio=更新了high的。"""
    alerts = []
    for h in portfolio.get("holdings", []):
        code = str(h["code"])
        sym, name = resolve_name(code)
        kl = fetch_kline(sym)
        if not kl or len(kl) < 21:
            continue
        price = kl[-1]["close"]
        prev_close = kl[-2]["close"] if len(kl) >= 2 else price
        cost = h["cost"]
        shares = h["shares"]
        h["high_since_buy"] = round(max(h.get("high_since_buy", cost), price), 3)
        high = h["high_since_buy"]
        pnl_pct = (price - cost) / cost
        h["current_price"] = price
        h["pnl_pct"] = round(pnl_pct * 100, 1)
        h["name"] = name

        m20 = ma(kl, 20)
        _, trend = ma_slope(kl)
        daily_chg = (price - prev_close) / prev_close if prev_close else 0
        trailing_drop = (high - price) / high if high > 0 else 0

        # 1. 硬止损：浮亏 >= -8%
        if pnl_pct <= HARD_STOP:
            alerts.append({"code": code, "name": name, "type": "硬止损",
                           "sell_shares": shares, "price": price,
                           "reason": f"浮亏{pnl_pct*100:.1f}%，触硬止损线-8%，全卖"})
        # 2. 极端波动：单日跌幅 > 5%
        elif daily_chg <= EXTREME_DROP:
            alerts.append({"code": code, "name": name, "type": "极端波动",
                           "sell_shares": shares, "price": price,
                           "reason": f"单日跌{daily_chg*100:.1f}%，极端波动全卖"})
        # 3. 趋势破坏：收盘价 < MA20 且 MA20 走平/向下
        elif m20 and price < m20 and trend in ("走平", "向下"):
            alerts.append({"code": code, "name": name, "type": "趋势破坏",
                           "sell_shares": shares, "price": price,
                           "reason": f"价格¥{price}跌破MA20¥{m20:.3f}且趋势{trend}，全卖"})
        # 4. 移动止盈：从最高价回撤 >= 6%（仅盈利时）
        elif pnl_pct > 0 and trailing_drop >= TRAILING_STOP:
            alerts.append({"code": code, "name": name, "type": "移动止盈",
                           "sell_shares": shares, "price": price,
                           "reason": f"从最高¥{high}回撤{trailing_drop*100:.1f}%，锁利全卖（浮盈{pnl_pct*100:.1f}%）"})
    return alerts, portfolio


def get_watchlist(path=None):
    """读 watchlist.txt，每行一个代码，# 及之后为注释。"""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.txt")
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()  # 去掉行内注释
            if not line:
                continue
            out.append(line)
    return out or ["512760", "512880", "510500", "588000", "512480", "159901"]
