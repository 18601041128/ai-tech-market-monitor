import json
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fetch_market import (
    build_universe,
    eastmoney_secid,
    tencent_symbol,
    http_text,
)

TZ = ZoneInfo("Asia/Shanghai")

CONFIG = Path("config.json")
INDICATORS = Path(
    "data/history/indicators.json"
)

BARS_DIR = Path(
    "data/history/bars"
)

LIMIT = 60


def safe_float(v):
    try:
        return float(v)
    except Exception:
        return None


def fetch_eastmoney_daily(item):

    params = urllib.parse.urlencode(
        {
            "secid": eastmoney_secid(item),
            "klt": "101",
            "fqt": "1",
            "beg": "0",
            "end": "20500000",
            "lmt": str(LIMIT),
            "fields1":
                "f1,f2,f3,f4,f5,f6",
            "fields2":
                "f51,f52,f53,f54,f55,"
                "f56,f57,f58,f59,f60,f61",
        }
    )

    url = (
        "https://push2his.eastmoney.com/"
        "api/qt/stock/kline/get?"
        + params
    )

    raw = http_text(
        url,
        timeout=10
    )

    payload = json.loads(raw)

    data = payload.get("data") or {}

    klines = data.get("klines") or []

    bars = []

    for line in klines:

        f = line.split(",")

        if len(f) < 7:
            continue

        bars.append(
            {
                "date": f[0],
                "open": safe_float(f[1]),
                "close": safe_float(f[2]),
                "high": safe_float(f[3]),
                "low": safe_float(f[4]),
                "volume": safe_float(f[5]),
                "amount": safe_float(f[6]),
                "amplitude_pct":
                    safe_float(f[7])
                    if len(f) > 7
                    else None,
                "pct":
                    safe_float(f[8])
                    if len(f) > 8
                    else None,
                "change":
                    safe_float(f[9])
                    if len(f) > 9
                    else None,
                "turnover_pct":
                    safe_float(f[10])
                    if len(f) > 10
                    else None,
            }
        )

    if len(bars) < 20:
        raise RuntimeError(
            "Eastmoney history too short"
        )

    return bars, "eastmoney"


def fetch_tencent_daily(item):

    symbol = tencent_symbol(item)

    url = (
        "https://web.ifzq.gtimg.cn/"
        "appstock/app/fqkline/get?"
        f"param={symbol},day,,,{LIMIT},qfq"
    )

    raw = http_text(
        url,
        timeout=10
    )

    payload = json.loads(raw)

    node = (
        payload.get("data", {})
        .get(symbol, {})
    )

    rows = (
        node.get("qfqday")
        or node.get("day")
        or []
    )

    bars = []

    for f in rows:

        if len(f) < 6:
            continue

        bars.append(
            {
                "date": f[0],
                "open": safe_float(f[1]),
                "close": safe_float(f[2]),
                "high": safe_float(f[3]),
                "low": safe_float(f[4]),
                "volume": safe_float(f[5]),
                "amount": None,
                "amplitude_pct": None,
                "pct": None,
                "change": None,
                "turnover_pct": None,
            }
        )

    if len(bars) < 20:
        raise RuntimeError(
            "Tencent history too short"
        )

    return bars, "tencent"


def fetch_daily(item):

    errors = []

    try:
        return fetch_eastmoney_daily(
            item
        )
    except Exception as e:
        errors.append(
            f"eastmoney: {e}"
        )

    try:
        return fetch_tencent_daily(
            item
        )
    except Exception as e:
        errors.append(
            f"tencent: {e}"
        )

    raise RuntimeError(
        " | ".join(errors)
    )


def avg(values):

    values = [
        x
        for x in values
        if x is not None
    ]

    if not values:
        return None

    return sum(values) / len(values)


def ma(bars, n):

    if len(bars) < n:
        return None

    values = [
        x["close"]
        for x in bars[-n:]
        if x["close"] is not None
    ]

    if len(values) < n:
        return None

    return sum(values) / n


def change_n(bars, n):

    if len(bars) <= n:
        return None

    current = bars[-1]["close"]
    old = bars[-1 - n]["close"]

    if (
        current is None
        or old in (None, 0)
    ):
        return None

    return (
        current / old - 1
    ) * 100


def atr14(bars):

    if len(bars) < 15:
        return None

    trs = []

    for i in range(
        1,
        len(bars)
    ):

        high = bars[i]["high"]
        low = bars[i]["low"]
        prev = bars[i - 1]["close"]

        if None in (
            high,
            low,
            prev
        ):
            continue

        tr = max(
            high - low,
            abs(high - prev),
            abs(low - prev),
        )

        trs.append(tr)

    if len(trs) < 14:
        return None

    return sum(
        trs[-14:]
    ) / 14


def indicators_for(bars):

    latest = bars[-1]

    last20 = bars[-20:]

    highs = [
        x["high"]
        for x in last20
        if x["high"] is not None
    ]

    lows = [
        x["low"]
        for x in last20
        if x["low"] is not None
    ]

    volumes = [
        x["volume"]
        for x in bars[-5:]
    ]

    amounts = [
        x["amount"]
        for x in bars[-5:]
    ]

    return {
        "latest_bar_date":
            latest["date"],

        "latest_close":
            latest["close"],

        "ma5":
            ma(bars, 5),

        "ma10":
            ma(bars, 10),

        "ma20":
            ma(bars, 20),

        "ma30":
            ma(bars, 30),

        "high20":
            max(highs)
            if highs
            else None,

        "low20":
            min(lows)
            if lows
            else None,

        "atr14":
            atr14(bars),

        "avg_volume5":
            avg(volumes),

        "avg_amount5":
            avg(amounts),

        "change_5d_pct":
            change_n(bars, 5),

        "change_10d_pct":
            change_n(bars, 10),

        "change_20d_pct":
            change_n(bars, 20),
    }


def desired_refresh_tag(now):

    # 第一次没有文件时任何时间都初始化
    if not INDICATORS.exists():
        return (
            now.date().isoformat()
            + "-bootstrap"
        )

    # 盘前更新：使用上一交易日完整日K
    if now.hour < 9:
        return (
            now.date().isoformat()
            + "-preopen"
        )

    # 收盘后更新：纳入当天日K
    if (
        now.hour > 15
        or (
            now.hour == 15
            and now.minute >= 5
        )
    ):
        return (
            now.date().isoformat()
            + "-postclose"
        )

    return None


def main():

    now = datetime.now(TZ)

    tag = desired_refresh_tag(now)

    if tag is None:
        print(
            "History refresh not needed now"
        )
        return

    if INDICATORS.exists():

        try:
            old = json.loads(
                INDICATORS.read_text(
                    encoding="utf-8"
                )
            )

            if (
                old.get("refresh_tag")
                == tag
            ):
                print(
                    "History already refreshed:",
                    tag
                )
                return

        except Exception:
            pass

    cfg = json.loads(
        CONFIG.read_text(
            encoding="utf-8"
        )
    )

    universe = build_universe(cfg)

    BARS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    results = {}
    errors = {}

    for item in universe:

        try:

            bars, source = (
                fetch_daily(item)
            )

            indicator = (
                indicators_for(bars)
            )

            results[
                item["code"]
            ] = {
                "code":
                    item["code"],
                "name":
                    item["name"],
                "kind":
                    item["kind"],
                "source":
                    source,
                **indicator,
            }

            bar_file = (
                BARS_DIR
                / f'{item["code"]}.json'
            )

            bar_file.write_text(
                json.dumps(
                    {
                        "code":
                            item["code"],
                        "name":
                            item["name"],
                        "source":
                            source,
                        "adjustment":
                            "qfq",
                        "bars":
                            bars,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        except Exception as e:

            errors[
                item["code"]
            ] = {
                "name":
                    item["name"],
                "error":
                    str(e),
            }

        time.sleep(0.08)

    payload = {
        "generated_at":
            now.isoformat(
                timespec="seconds"
            ),
        "refresh_tag":
            tag,
        "expected":
            len(universe),
        "received":
            len(results),
        "failed":
            len(errors),
        "indicators":
            results,
        "errors":
            errors,
    }

    INDICATORS.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    INDICATORS.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "expected":
                    len(universe),
                "received":
                    len(results),
                "failed":
                    len(errors),
                "refresh_tag":
                    tag,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
