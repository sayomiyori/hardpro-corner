"""Collect a private catalog with MCP metadata and read-only prices from 1C."""
import argparse
import json
import os
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from onec_live import configured_reader
from manager_reports import MSK

BATCH_SIZE = 24


def read_metadata(path):
    result = {}
    with path.open(encoding="utf-8-sig") as source:
        for line in source:
            row = json.loads(line)
            code = row["code"]
            if not code or code in result:
                raise ValueError("Неверный или повторяющийся код товара в выгрузке MCP")
            result[code] = row
    return result


def with_retry(fetch):
    for delay in (0, 2, 5):
        if delay:
            time.sleep(delay)
        try:
            return fetch()
        except OSError:
            if delay == 5:
                raise


def collect(reader, metadata):
    catalog = with_retry(reader.catalog)
    indexed_at = datetime.now(MSK)
    by_product = defaultdict(set)
    used_codes = set()
    for row in catalog.rows:
        code = row["код"]
        if metadata.get(code, {}).get("title") != row["товар"]:
            raise ValueError("Состав каталога MCP и читателя 1С расходится")
        used_codes.add(code)
        by_product[row["product"]].add(row["id"])
    if used_codes != metadata.keys() or len(by_product) != len(metadata):
        raise ValueError("Каталог и выгрузка MCP содержат разные товары")

    products = list(by_product)
    offers = {}
    priced_variants = 0
    priced_products = set()
    for offset in range(0, len(products), BATCH_SIZE):
        batch = products[offset:offset + BATCH_SIZE]
        stamp, balance, prices = with_retry(lambda: reader.offers(batch, require_prices=True))
        for product in batch:
            keys = by_product[product]
            extra_prices = {variant: price for (owner, variant), price in prices.items()
                            if owner == product and variant not in keys and price is not None}
            offers[product] = {"as_of": stamp.isoformat(), "variants": {},
                               "other_retail_prices_rub": {k: str(v) for k, v in extra_prices.items()}}
            for variant in sorted(keys):
                key = product, variant
                price = prices.get(key)
                if price is not None:
                    priced_variants += 1
                    priced_products.add(product)
                offers[product]["variants"][variant] = {
                    "retail_rub": str(price) if price is not None else None,
                    "free": {name: str(balance[key][uid]) for name, uid in reader.stores.items()
                             if uid in balance.get(key, {})},
                }
        if not (offset // BATCH_SIZE) % 20 or offset + BATCH_SIZE >= len(products):
            print(f"Цены и остатки: {min(offset + BATCH_SIZE, len(products))}/{len(products)} товаров", flush=True)

    totals = {"products": len(products), "variants": len(catalog.rows),
              "products_with_retail_price": len(priced_products), "variants_with_retail_price": priced_variants}
    return {"schema": 1, "generated_at": datetime.now(MSK).isoformat(), "totals": totals,
            "catalog": {"момент": indexed_at.strftime("%d.%m.%Y %H:%M МСК"),
                        "склады": list(catalog.stores), "товары": catalog.rows},
            "metadata_by_code": metadata, "offers_by_product": offers}


def save(output, data):
    descriptor, name = tempfile.mkstemp(dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(name, output)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("metadata", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    data = collect(configured_reader(os.environ["ONEC_READER_CONFIG"]), read_metadata(args.metadata))
    save(args.output, data)
    print(json.dumps(data["totals"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
