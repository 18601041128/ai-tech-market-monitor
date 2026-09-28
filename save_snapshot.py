import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

LIVE_FILE = Path("data/etf_live.json")
CONFIG_FILE = Path("config.json")

SNAPSHOT_SLOTS = [
    "09:35",
    "10:05",
    "10:35",
    "11:05",
    "11:35",   # 上午正式收盘快照
    "13:05",
    "13:35",
    "14:05",
    "14:35",
    "15:05",   # 全天正式收盘快照
]

MAX_LAG_MINUTES = 20


def round_num(v, n=4):
    if v is None:
        return None
    try:
        return round(float(v), n)
    except Exception:
        return None


def pct_change(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


def load_json(path):
    return json.loads(
        path.read_text(encoding="utf-8")
    )


def get_slot(now):
    latest = None

    for slot in SNAPSHOT_SLOTS:
        h, m = map(int, slot.split(":"))

        slot_dt = now.replace(
            hour=h,
            minute=m,
            second=0,
            microsecond=0,
        )

        if slot_dt <= now:
            latest = (slot, slot_dt)

    if latest is None:
        return None

    slot, slot_dt = latest

    lag = (
        now - slot_dt
    ).total_seconds() / 60

    if lag > MAX_LAG_MINUTES:
        return None

    return slot, slot_dt, lag


def add_quote_derived(quotes):

    benchmarks = {
        "sh": quotes.get("000001", {}).get("pct"),
        "sz": quotes.get("399001", {}).get("pct"),
        "cyb": quotes.get("399006", {}).get("pct"),
        "star50": quotes.get("000688", {}).get("pct"),
    }

    for code, q in quotes.items():

        price = q.get("price")
        high = q.get("high")
        low = q.get("low")
        op = q.get("open")
        prev = q.get("prev_close")
        pct = q.get("pct")

        derived = {}

        derived["from_low_pct"] = round_num(
            pct_change(price, low)
        )

        derived["from_high_pct"] = round_num(
            pct_change(price, high)
        )

        derived["gap_pct"] = round_num(
            pct_change(op, prev)
        )

        if (
            price is not None
            and high is not None
            and low is not None
            and high > low
        ):
            derived["range_position_pct"] = round_num(
                (price - low)
                / (high - low)
                * 100
            )
        else:
            derived["range_position_pct"] = None

        if pct is not None:

            derived["relative_to_sh"] = (
                None
                if benchmarks["sh"] is None
                else round_num(
                    pct - benchmarks["sh"]
                )
            )

            derived["relative_to_sz"] = (
                None
                if benchmarks["sz"] is None
                else round_num(
                    pct - benchmarks["sz"]
                )
            )

            derived["relative_to_cyb"] = (
                None
                if benchmarks["cyb"] is None
                else round_num(
                    pct - benchmarks["cyb"]
                )
            )

            derived["relative_to_star50"] = (
                None
                if benchmarks["star50"] is None
                else round_num(
                    pct - benchmarks["star50"]
                )
            )

        q["derived"] = derived

    return benchmarks


def build_core_resonance(cfg, quotes):

    result = {}

    for etf in cfg["etfs"]:

        etf_code = etf["code"]
        etf_quote = quotes.get(etf_code, {})
        etf_pct = etf_quote.get("pct")

        members = []

        for stock in etf.get("stocks", []):

            q = quotes.get(stock["code"])

            if not q:
                continue

            members.append(
                {
                    "code": stock["code"],
                    "name": stock["name"],
                    "role": stock.get(
                        "type",
                        "holding"
                    ),
                    "pct": q.get("pct"),
                    "price": q.get("price"),
                    "status": q.get("status"),
                }
            )

        valid_pcts = [
            x["pct"]
            for x in members
            if x["pct"] is not None
        ]

        positive_count = sum(
            1
            for x in valid_pcts
            if x > 0
        )

        if etf_pct is not None:
            outperform_etf_count = sum(
                1
                for x in valid_pcts
                if x > etf_pct
            )
        else:
            outperform_etf_count = None

        avg_pct = (
            sum(valid_pcts) / len(valid_pcts)
            if valid_pcts
            else None
        )

        result[etf_code] = {
            "etf_name": etf["name"],
            "theme": etf.get("theme"),
            "member_count": len(valid_pcts),
            "positive_count": positive_count,
            "outperform_etf_count":
                outperform_etf_count,
            "average_pct": round_num(avg_pct),
            "members": members,
        }

    return result


def main():

    if not LIVE_FILE.exists():
        print("live file not found")
        return

    now = datetime.now(TZ)

    slot_info = get_slot(now)

    if slot_info is None:
        print(
            "Not inside snapshot window:",
            now.isoformat()
        )
        return

    slot, slot_dt, lag = slot_info

    date_str = now.date().isoformat()
    slot_name = slot.replace(":", "")

    folder = (
        Path("data/snapshots")
        / date_str
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    output = folder / f"{slot_name}.json"

    # 已保存则不重复覆盖
    if output.exists():
        print(
            f"Snapshot already exists: {output}"
        )
        return

    payload = load_json(LIVE_FILE)
    cfg = load_json(CONFIG_FILE)

    quotes = payload.get("quotes", {})

    benchmarks = add_quote_derived(
        quotes
    )

    etfs = [
        q
        for q in quotes.values()
        if q.get("kind") == "etf"
        and q.get("pct") is not None
    ]

    etf_rank = sorted(
        [
            {
                "code": x["code"],
                "name": x["name"],
                "price": x.get("price"),
                "pct": x.get("pct"),
                "status": x.get("status"),
            }
            for x in etfs
        ],
        key=lambda x: x["pct"],
        reverse=True,
    )

    core_resonance = build_core_resonance(
        cfg,
        quotes
    )

    snapshot = {
        "snapshot_slot": slot,
        "scheduled_time":
            slot_dt.isoformat(
                timespec="seconds"
            ),
        "captured_at":
            now.isoformat(
                timespec="seconds"
            ),
        "capture_lag_minutes":
            round(lag, 2),
        "market_data_fetched_at":
            payload.get("fetched_at"),
        "market_data_date":
            payload.get("trading_date"),
        "counts":
            payload.get("counts"),
        "benchmarks":
            benchmarks,
        "etf_rank_by_pct":
            etf_rank,
        "core_resonance":
            core_resonance,
        "quotes":
            quotes,
    }

    output.write_text(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8",
    )

    latest = Path(
        "data/snapshots/latest.json"
    )

    latest.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    latest.write_text(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8",
    )

    print(
        f"Saved snapshot {slot} -> {output}"
    )


if __name__ == "__main__":
    main()
