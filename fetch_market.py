import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG = Path("config.json")
OUTPUT = Path("data/etf_live.json")

TZ = ZoneInfo("Asia/Shanghai")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/130 Safari/537.36"
)

# 东方财富只用于ETF第二核验，避免拖慢全部27个标的
EM_HOSTS = [
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
]

EM_FIELDS = (
    "f43,f44,f45,f46,f47,f48,"
    "f57,f58,f60,f86,f168,f169,f170"
)

EM_UT = "fa5fd1943c7b386f172d6893dbbd1d0c"


def now_cn():
    return datetime.now(TZ)


def http_text(url, encoding="utf-8", timeout=3):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": "https://quote.eastmoney.com/",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(
            encoding,
            errors="replace"
        )


def to_float(v):
    if v in (None, "", "-", "--"):
        return None

    try:
        return float(v)
    except Exception:
        return None


def normalize_vendor_time(v):
    if v in (None, ""):
        return None

    s = str(v).strip()

    try:
        if s.isdigit() and len(s) == 14:
            dt = datetime.strptime(
                s,
                "%Y%m%d%H%M%S"
            ).replace(tzinfo=TZ)

            return dt.isoformat(
                timespec="seconds"
            )

        if s.isdigit() and len(s) == 12:
            dt = datetime.strptime(
                s,
                "%Y%m%d%H%M"
            ).replace(tzinfo=TZ)

            return dt.isoformat(
                timespec="seconds"
            )

        if s.isdigit() and len(s) == 10:
            dt = datetime.fromtimestamp(
                int(s),
                tz=TZ
            )

            return dt.isoformat(
                timespec="seconds"
            )

    except Exception:
        pass

    return s


def eastmoney_secid(item):
    prefix = (
        "1."
        if item["market"] == "SH"
        else "0."
    )

    return prefix + item["code"]


def tencent_symbol(item):
    prefix = (
        "sh"
        if item["market"] == "SH"
        else "sz"
    )

    return prefix + item["code"]


def build_universe(cfg):
    universe = {}

    for etf in cfg["etfs"]:

        code = etf["code"]

        universe[code] = {
            "code": code,
            "name": etf["name"],
            "market": etf["market"],
            "kind": "etf",
            "theme": etf.get("theme"),
            "relations": [],
        }

        for stock in etf.get("stocks", []):

            code = stock["code"]

            if code not in universe:
                universe[code] = {
                    "code": code,
                    "name": stock["name"],
                    "market": (
                        "SH"
                        if code.startswith("6")
                        else "SZ"
                    ),
                    "kind": "stock",
                    "theme": None,
                    "relations": [],
                }

            universe[code][
                "relations"
            ].append(
                {
                    "parent_etf":
                        etf["code"],
                    "parent_name":
                        etf["name"],
                    "role":
                        stock.get(
                            "type",
                            "holding"
                        ),
                }
            )

    for idx in cfg.get("indices", []):

        universe[
            idx["code"]
        ] = {
            "code": idx["code"],
            "name": idx["name"],
            "market": idx["market"],
            "kind": "index",
            "theme": "benchmark",
            "relations": [],
        }

    return list(universe.values())


# =========================================================
# 腾讯：一次请求批量抓取全部标的
# =========================================================

def parse_tencent_line(line):

    if '"' not in line:
        return None, None

    left, right = line.split('"', 1)

    symbol = (
        left.replace("v_", "")
        .replace("=", "")
        .strip()
    )

    body = right.rsplit('"', 1)[0]

    f = body.split("~")

    if len(f) < 35:
        return symbol, None

    price = to_float(f[3])

    if price is None or price <= 0:
        return symbol, None

    amount = None

    if len(f) > 37:
        amount_wan = to_float(f[37])

        if amount_wan is not None:
            amount = amount_wan * 10000

    quote = {
        "source": "tencent",

        "code": f[2],

        "name": f[1],

        "price": price,

        "pct": to_float(f[32]),

        "change": to_float(f[31]),

        "open": to_float(f[5]),

        "high": to_float(f[33]),

        "low": to_float(f[34]),

        "prev_close": to_float(f[4]),

        "volume_raw": to_float(f[6]),

        "amount": amount,

        "turnover_pct": (
            to_float(f[38])
            if len(f) > 38
            else None
        ),

        "source_time":
            normalize_vendor_time(
                f[30]
            ),
    }

    return symbol, quote


def fetch_tencent_batch(universe):

    symbols = [
        tencent_symbol(item)
        for item in universe
    ]

    url = (
        "https://qt.gtimg.cn/q="
        + ",".join(symbols)
    )

    text = http_text(
        url,
        encoding="gbk",
        timeout=5,
    )

    results = {}

    for line in text.splitlines():

        symbol, quote = (
            parse_tencent_line(line)
        )

        if not symbol or not quote:
            continue

        code = symbol[-6:]

        results[code] = quote

    return results


# =========================================================
# 东方财富：只核验7只ETF，且并发执行
# =========================================================

def fetch_eastmoney_fast(item):

    params = urllib.parse.urlencode(
        {
            "secid":
                eastmoney_secid(item),

            "fields":
                EM_FIELDS,

            "fltt": "2",

            "invt": "2",

            "ut": EM_UT,
        }
    )

    last_error = None

    for host in EM_HOSTS:

        try:

            url = (
                f"{host}/api/qt/"
                f"stock/get?{params}"
            )

            raw = http_text(
                url,
                timeout=2
            )

            payload = json.loads(raw)

            d = (
                payload.get("data")
                or {}
            )

            price = to_float(
                d.get("f43")
            )

            if (
                not d
                or price is None
                or price <= 0
            ):
                raise ValueError(
                    "empty quote"
                )

            return {
                "source":
                    "eastmoney",

                "code":
                    str(
                        d.get("f57")
                        or item["code"]
                    ),

                "name":
                    (
                        d.get("f58")
                        or item["name"]
                    ),

                "price":
                    price,

                "pct":
                    to_float(
                        d.get("f170")
                    ),

                "change":
                    to_float(
                        d.get("f169")
                    ),

                "open":
                    to_float(
                        d.get("f46")
                    ),

                "high":
                    to_float(
                        d.get("f44")
                    ),

                "low":
                    to_float(
                        d.get("f45")
                    ),

                "prev_close":
                    to_float(
                        d.get("f60")
                    ),

                "volume_raw":
                    to_float(
                        d.get("f47")
                    ),

                "amount":
                    to_float(
                        d.get("f48")
                    ),

                "turnover_pct":
                    to_float(
                        d.get("f168")
                    ),

                "source_time":
                    normalize_vendor_time(
                        d.get("f86")
                    ),
            }

        except Exception as e:

            last_error = str(e)

    raise RuntimeError(
        last_error
        or "Eastmoney failed"
    )


def fetch_etf_crosschecks(universe):

    etfs = [
        x
        for x in universe
        if x["kind"] == "etf"
    ]

    results = {}
    errors = {}

    with ThreadPoolExecutor(
        max_workers=7
    ) as pool:

        futures = {
            pool.submit(
                fetch_eastmoney_fast,
                item
            ): item
            for item in etfs
        }

        for future in as_completed(
            futures
        ):

            item = futures[future]

            try:
                results[
                    item["code"]
                ] = future.result()

            except Exception as e:
                errors[
                    item["code"]
                ] = str(e)

    return results, errors


# =========================================================
# 数据合并和核验
# =========================================================

def price_tolerance(item, price):

    if item["kind"] == "etf":
        return 0.001

    if item["kind"] == "index":
        return max(
            0.5,
            price * 0.0002
        )

    return max(
        0.01,
        price * 0.0005
    )


def merge_quotes(
    item,
    tx,
    em=None
):

    if tx is None and em is None:
        return None

    # 腾讯是快速稳定主源
    result = dict(
        tx if tx else em
    )

    result["status"] = (
        "single_source"
    )

    result["crosscheck"] = None

    if tx and em:

        price_diff = abs(
            tx["price"]
            - em["price"]
        )

        pct_diff = None

        if (
            tx.get("pct") is not None
            and em.get("pct") is not None
        ):
            pct_diff = abs(
                tx["pct"]
                - em["pct"]
            )

        conflict = (
            price_diff
            > price_tolerance(
                item,
                tx["price"]
            )
        )

        if (
            pct_diff is not None
            and pct_diff > 0.10
        ):
            conflict = True

        result = dict(tx)

        result["status"] = (
            "data_conflict"
            if conflict
            else "verified_2_sources"
        )

        result["crosscheck"] = {
            "secondary_source":
                "eastmoney",

            "secondary_price":
                em.get("price"),

            "secondary_pct":
                em.get("pct"),

            "price_diff":
                round(
                    price_diff,
                    6
                ),

            "pct_diff":
                (
                    None
                    if pct_diff is None
                    else round(
                        pct_diff,
                        4
                    )
                ),

            "secondary_source_time":
                em.get(
                    "source_time"
                ),
        }

    source_time = result.get(
        "source_time"
    )

    result["source_date"] = None

    if (
        isinstance(
            source_time,
            str
        )
        and len(source_time) >= 10
        and source_time[4] == "-"
    ):
        result["source_date"] = (
            source_time[:10]
        )

    return result


def main():

    started = now_cn()

    cfg = json.loads(
        CONFIG.read_text(
            encoding="utf-8"
        )
    )

    universe = build_universe(cfg)

    # 1. 腾讯批量抓全部27个
    try:
        tx_quotes = (
            fetch_tencent_batch(
                universe
            )
        )
        tx_error = None

    except Exception as e:
        tx_quotes = {}
        tx_error = str(e)

    # 2. 东方财富只并发核验7只ETF
    em_quotes, em_errors = (
        fetch_etf_crosschecks(
            universe
        )
    )

    rows = {}
    errors = {}

    for item in universe:

        code = item["code"]

        tx = tx_quotes.get(code)
        em = em_quotes.get(code)

        merged = merge_quotes(
            item,
            tx,
            em
        )

        if merged:

            rows[code] = {
                "code":
                    code,

                "name":
                    item["name"],

                "market":
                    item["market"],

                "kind":
                    item["kind"],

                "theme":
                    item.get("theme"),

                "relations":
                    item.get(
                        "relations",
                        []
                    ),

                **merged,
            }

        else:

            errors[code] = {
                "name":
                    item["name"],

                "tencent":
                    tx_error,

                "eastmoney":
                    em_errors.get(code),
            }

    counts = {
        "expected":
            len(universe),

        "received":
            len(rows),

        "verified_2_sources":
            sum(
                1
                for x in rows.values()
                if x["status"]
                == "verified_2_sources"
            ),

        "single_source":
            sum(
                1
                for x in rows.values()
                if x["status"]
                == "single_source"
            ),

        "data_conflict":
            sum(
                1
                for x in rows.values()
                if x["status"]
                == "data_conflict"
            ),

        "failed":
            len(errors),
    }

    finished = now_cn()

    runtime_seconds = (
        finished - started
    ).total_seconds()

    payload = {
        "schema_version": 2,

        "timezone":
            "Asia/Shanghai",

        "fetched_at":
            finished.isoformat(
                timespec="seconds"
            ),

        "trading_date":
            finished.date()
            .isoformat(),

        "runtime_seconds":
            round(
                runtime_seconds,
                2
            ),

        "counts":
            counts,

        "quotes":
            rows,

        "errors":
            errors,
    }

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_file = (
        OUTPUT.with_suffix(
            ".tmp"
        )
    )

    temp_file.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    temp_file.replace(
        OUTPUT
    )

    print(
        json.dumps(
            {
                **counts,
                "runtime_seconds":
                    round(
                        runtime_seconds,
                        2
                    ),
            },
            ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
