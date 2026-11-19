"""Build the buyer catalogue from a verified export and the archived MODX dump."""
import argparse
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

SQL_TOKEN = re.compile(r"'(?:\\.|''|[^'\\])*'|[^,()]+|[(),]")


def published_products(dump):
    columns, published = [], set()
    in_schema = False
    with dump.open(encoding="utf-8") as lines:
        for line in lines:
            if line.startswith("CREATE TABLE `modx_site_content`"):
                in_schema = True
                continue
            if in_schema:
                match = re.match(r"\s*`([^`]+)`", line)
                if match:
                    columns.append(match[1])
                if line.startswith(")"):
                    in_schema = False
            if not line.startswith("INSERT INTO `modx_site_content` VALUES "):
                continue
            row = None
            for match in SQL_TOKEN.finditer(line.split(" VALUES ", 1)[1]):
                value = match[0]
                if value == "(":
                    if row is not None:
                        raise ValueError("Invalid MODX export")
                    row = []
                elif value == ")":
                    if row is None or len(columns) != len(row):
                        raise ValueError("Invalid MODX column count")
                    item = dict(zip(columns, row))
                    if (item["published"] == "1" and item["deleted"] == "0" and
                        item["context_key"] == "web" and item["class_key"].endswith("msProduct")):
                        published.add(int(item["id"]))
                    row = None
                elif row is not None and value != ",":
                    row.append(value.strip("'"))
    return published


def project(catalog, website, reference, published):
    products = {r["code"]: r["uuid"] for r in reference["rows"] if not r["deleted"] and not r["is_group"]}
    active = set()
    for row in website["variants"]:
        product_id = int(row["product_id"])
        product, _, variant = row["uuid"].partition("#")
        if (int(row["active"]) == 1 and product_id in published and variant
                and product == website["products"].get(str(product_id))):
            active.add((product, variant))
    output, codes = [], {}
    for row in catalog["товары"]:
        if (products.get(row["код"]), row["id"]) not in active or row["вариант"].startswith("Характеристика без"):
            continue
        balance = {}
        for city in catalog["склады"]:
            qty = row["склады"].get(city, {}).get("свободно")
            balance[city] = "unknown" if qty is None else "available" if Decimal(qty) > 0 else "unavailable"
        code = codes.setdefault(row["код"], f"P{len(codes)+1:04d}")
        output.append({**{k: row[k] for k in ("товар", "артикул", "вариант")}, "код": code,
                       "id": str(len(output)+1), "склады": balance})
    if not output:
        raise ValueError("No matching public variants. Check source compatibility.")
    return {**{k: catalog[k] for k in ("момент", "склады")}, "товары": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["catalog", "evidence_audit", "modx_dump", "output"]:
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    audit = json.loads(args.evidence_audit.read_text(encoding="utf-8-sig").splitlines()[-1])
    cap = Path(audit["capture_directory"])
    def checked(name, sha):
        raw = (cap/name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError(f"Checksum differs: {name}")
        return json.loads(raw.decode("utf-8-sig"))
    hashes = {r["path"]: r["sha256"] for r in checked("evidence-manifest.json", audit["manifest_sha256"])["files"]}
    site = checked("website-snapshot.json", hashes["website-snapshot.json"])
    reference = checked("catalog-reference-manifest.json", hashes["catalog-reference-manifest.json"])
    result = project(json.loads(args.catalog.read_text(encoding="utf-8-sig")), site, reference, published_products(args.modx_dump))
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(len(result["товары"]), "buyer variants")


if __name__ == "__main__":
    main()
