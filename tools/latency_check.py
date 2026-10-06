"""
Measures response time against the < 3 s non-functional requirement.

Needs the action server and Rasa running (see README).
Usage:  python tools/latency_check.py --runs 5
Writes: results/latency.csv  and prints mean / p95 / max per turn.
"""
import argparse
import csv
import statistics
import time
import uuid
from pathlib import Path

import requests

URL = "http://localhost:5005/webhooks/rest/webhook"
FLOW = ["/plan_trip", "Lisbon", "Berlin", "2030-05-10", "900", "as green as possible",
        "what is CO2e?", "can I offset my emissions?", "asdkjh qwpoei", "talk to a human"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--url", default=URL)
    args = parser.parse_args()

    rows = []
    for run in range(args.runs):
        sender = f"latency-{uuid.uuid4().hex[:8]}"
        for turn, message in enumerate(FLOW, start=1):
            start = time.perf_counter()
            resp = requests.post(args.url, json={"sender": sender, "message": message}, timeout=60)
            resp.raise_for_status()
            rows.append({"run": run + 1, "turn": turn, "message": message,
                         "seconds": round(time.perf_counter() - start, 3)})

    Path("results").mkdir(exist_ok=True)
    with open("results/latency.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["run", "turn", "message", "seconds"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"{'turn':<5}{'message':<30}{'mean':>7}{'p95':>7}{'max':>7}")
    for turn, message in enumerate(FLOW, start=1):
        values = sorted(r["seconds"] for r in rows if r["turn"] == turn)
        p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
        print(f"{turn:<5}{message[:28]:<30}{statistics.mean(values):>7.2f}{p95:>7.2f}{max(values):>7.2f}")
    worst = max(r["seconds"] for r in rows)
    print(f"\nSlowest single response: {worst:.2f} s  ->  {'PASS' if worst < 3 else 'FAIL'} (< 3 s target)")


if __name__ == "__main__":
    main()
