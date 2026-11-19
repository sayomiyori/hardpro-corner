"""Выбрать разрешённые варианты и розничные склады из проверенной выгрузки 1С."""
import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def collect(capture, manifest_digest, selection):
    evidence = capture / "evidence-manifest.json"
    if hashlib.sha256(evidence.read_bytes()).hexdigest() != manifest_digest:
        raise ValueError("Не совпала контрольная сумма манифеста")
    manifest = read_json(evidence)
    hashes = {entry["path"]: entry["sha256"] for entry in manifest["files"]}

    def verified(name):
        raw = (capture / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != hashes[name]:
            raise ValueError(f"Не совпала контрольная сумма {name}")
        return json.loads(raw.decode("utf-8-sig"))

    connection = verified("connection-info.json")
    if any(connection[phase]["profile"] != "work" or connection[phase]["configuration"] != "Розница"
           for phase in ("before", "after")):
        raise ValueError("Источник не соответствует рабочей Рознице")
    items = verified("catalog-reference-manifest.json")["rows"]
    variants = verified("characteristics-snapshot.json")
    stocks = verified("stocks-snapshot.json")
    if not all(ds["complete_for_declared_scope"] for ds in (stocks, variants)):
        raise ValueError("Выгрузка неполна для своей области чтения")
    stores = selection["warehouses"]
    wanted = {(x["product_uuid"], x["characteristic_uuid"]) for x in selection["variants"]}
    if not wanted or len(wanted) != len(selection["variants"]) or not stores or len(set(stores.values())) != len(stores):
        raise ValueError("Пустой или неоднозначный отбор")
    products = {r["uuid"]: r for r in items if not r["deleted"] and not r["is_group"]}
    names = {(r["product_uuid"], r["characteristic_uuid"]): r["name"] for r in variants["rows"] if not r["deleted"]}
    balances = {}
    for row in stocks["rows"]:
        pair = (row["product_uuid"], row["characteristic_uuid"])
        warehouse = row["physical_warehouse_uuid"]
        if pair not in wanted or warehouse not in stores:
            continue
        key = (*pair, warehouse)
        if key in balances:
            raise ValueError("Повтор строки регистра по варианту и складу")
        gross, held = (Decimal(str(row[k])) for k in ("on_hand", "reserved"))
        if not gross.is_finite() or not held.is_finite():
            raise ValueError("Некорректное количество")
        balances[key] = {"физически": str(gross), "резерв": str(held), "свободно": str(gross - held)}
    as_of = datetime.fromisoformat(stocks["finished_utc"].replace("Z", "+00:00"))
    label = as_of.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК")
    result = {"источник": "Архивная выгрузка 1С", "момент": label, "дата_чтения": stocks["finished_utc"],
              "склады": list(stores.values()), "товары": []}
    for entry in selection["variants"]:
        pair = (entry["product_uuid"], entry["characteristic_uuid"])
        item = products[pair[0]]
        if item["code"] not in stocks["completed_codes"] or item["code"] not in variants["completed_codes"]:
            raise ValueError("Товар вне подтверждённого чтения")
        result["товары"].append({"товар": item["name"], "артикул": item["article"], "вариант": names[pair],
                                 "склады": {name: balances.get((*pair, uid), {"статус": "нет строки в выгрузке"})
                                            for uid, name in stores.items()}})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("capture", "selection", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    data = collect(args.capture, args.manifest_sha256, read_json(args.selection))
    with tempfile.NamedTemporaryFile(dir=args.output.parent, mode="w", encoding="utf-8", delete=False) as tmp:
        try:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp.close()
            os.replace(tmp.name, args.output)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
