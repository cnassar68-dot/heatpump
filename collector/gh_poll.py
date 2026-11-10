#!/usr/bin/env python3
"""One GitHub Actions run: take a few readings from the SPRSUN cloud, then write the
website's data file. History is kept in collector/data/history.db between runs.

  python gh_poll.py --samples 4 --out ../site/api/data
Username and password come from the SPRSUN_USERNAME / SPRSUN_PASSWORD secrets.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import collector  # noqa: E402
import sprsun_source  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "site", "api", "data"))
    args = ap.parse_args()

    cfg_path = os.path.join(HERE, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"cloud": {"base_url": "https://m2m.sprsun.com/JCloud",
                             "app_key": "6ee9d89011d5405a41292510afa3dc75",
                             "username": os.environ["SPRSUN_USERNAME"],
                             "password": os.environ["SPRSUN_PASSWORD"]},
                   "poll_seconds": 60, "offline_after_seconds": 300}, fh)
    try:
        cfg = collector.cloud_config(cfg_path)
    finally:
        os.remove(cfg_path)

    store = collector.Store(cfg["database"])
    amaps = {}
    ok = 0
    for i in range(args.samples):
        started = time.time()
        try:
            devices, n = sprsun_source.poll_once(cfg, store, amaps)
            ok += 1
            print("sample %d: %d unit(s)" % (i + 1, n))
        except Exception as err:
            print("sample %d failed: %s" % (i + 1, err))
        if i < args.samples - 1:
            time.sleep(max(5.0, 60 - (time.time() - started)))
    store.prune(int(time.time() * 1000) - cfg["retention_days"] * 86400000)
    cfg["units"] = store.known_units()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(collector.build_api(cfg, store), fh, separators=(",", ":"))
    print("units: %d, good samples: %d" % (len(cfg["units"]), ok))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
