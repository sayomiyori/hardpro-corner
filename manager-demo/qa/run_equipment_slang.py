"""Verify the 50 equipment-language cases against the manager's normal reply path."""

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen


def api_reply(base, turns):
    body = {"model": "hardpro-manager", "messages": [{"role": "user", "content": text} for text in turns]}
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["MANAGER_TOKEN"]}
    request = Request(base.rstrip("/") + "/manager/v1/chat/completions",
                      data=json.dumps(body).encode(), headers=headers)
    with urlopen(request, timeout=45) as response:
        return json.load(response)["choices"][0]["message"]["content"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--url", help="Catalog API base URL. Otherwise imports staged catalog_api.")
    parser.add_argument("--ids", nargs="+", help="Run only the specified question identifiers.")
    args = parser.parse_args()
    fixture = json.loads(Path(__file__).with_name("equipment-slang-50.json").read_text(encoding="utf-8"))
    if not args.url:
        import catalog_api

        catalog_api.LIVE_SNAPSHOT = args.out.parent / "qa-live-index.json"

    results = []
    for case in fixture["questions"]:
        if args.ids and case["id"] not in args.ids:
            continue
        turns = [*fixture["contexts"].get(case.get("context"), []), case["question"]]
        start = time.monotonic()
        try:
            answer = api_reply(args.url, turns) if args.url else catalog_api.reply("manager", turns)
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            answer = f"Ошибка запроса ({type(exc).__name__})."
        checks = {
            "missing": [rule for rule in case["want"] if not re.search(rule, answer, re.I)],
            "unexpected": [rule for rule in case["ban"] if re.search(rule, answer, re.I)],
        }
        status = "FAIL" if any(checks.values()) else "PASS"
        results.append({**case, "status": status, "seconds": round(time.monotonic() - start, 2),
                        "answer": answer, **checks})
        print(case["id"], status, results[-1]["seconds"], *checks.values(), flush=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    args.out.chmod(0o600)
    print(f"{sum(row['status'] == 'PASS' for row in results)}/{len(results)}")


if __name__ == "__main__":
    main()
