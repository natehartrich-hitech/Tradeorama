"""Command line: python -m tradeorama.mm {run,report,status}

run     collect Kalshi order books and trades for the rewarded markets (paper only; resumes after a restart)
report  score the test against the rules fixed before the run
status  how far along it is
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from ..replay.kalshi import Kalshi
from .collect import Collector, heartbeat_from_env
from .report import report
from .store import Store

DEFAULT_DB = "mm.db"


def cmd_run(args, out=print) -> int:
    hb = heartbeat_from_env()
    store = Store(args.db)
    collector = Collector(Kalshi(min_interval=args.interval), store, days=args.days, heartbeat=hb, out=out)
    try:
        return 0 if collector.run() in ("done", "stopped") else 1
    except KeyboardInterrupt:
        out("Stopped. Run again to resume where it left off.")
        return 0
    except Exception as exc:  # report a crash to the heartbeat check right away
        hb.fail(f"{type(exc).__name__}: {exc}")
        raise


def cmd_status(args, out=print) -> int:
    store = Store(args.db)
    c = store.counts()
    started, ends = store.get("started"), store.get("ends")
    if not started:
        out("Not started yet. Run: python -m tradeorama.mm run")
        return 0
    f = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out(f"Started {f(started)}, ends {f(ends)}. Following {c['active']} markets ({c['markets']} so far). "
        f"Stored {c['books']:,} book snapshots and {c['trades']:,} trades"
        + (f"; last snapshot {f(c['last'])}." if c["last"] else "."))
    return 0


def cmd_report(args, out=print) -> int:
    report(Store(args.db), out)
    return 0


def main(argv=None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m tradeorama.mm", description="Paper test of market making for Kalshi's rewards")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="collect data for the test (paper only; resumes after a restart)")
    r.add_argument("--days", type=float, default=5, help="test length, set on the first start (default 5)")
    r.add_argument("--interval", type=float, default=0.25, help="shortest gap between Kalshi requests (default 0.25 s)")
    r.add_argument("--db", default=DEFAULT_DB)
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("status", help="how far along the test is")
    s.add_argument("--db", default=DEFAULT_DB)
    s.set_defaults(fn=cmd_status)
    rep = sub.add_parser("report", help="score the test against its rules")
    rep.add_argument("--db", default=DEFAULT_DB)
    rep.add_argument("--out", help="also save the report to this file, as UTF-8")
    rep.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    if getattr(args, "out", None):
        with open(args.out, "w", encoding="utf-8") as fh:
            def tee(line: str) -> None:
                print(line)
                fh.write(line + "\n")
            code = args.fn(args, out=tee)
        print(f"Saved to {args.out}")
    else:
        code = args.fn(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
