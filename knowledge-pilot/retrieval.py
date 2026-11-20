"""Local retrieval pilot for sourced product and equipment guidance."""

import argparse
import json
import math
import re
import statistics
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Literal
from urllib.parse import urlparse

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient, models

FOLDER = Path(__file__).resolve().parent
COLLECTION = "equipment_guides"
DENSE_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SPARSE_MODEL = "Qdrant/bm25"
DIMENSIONS = 384
MODES = ("lexical", "dense", "hybrid")
TOP_K = 3
PREFETCH_K = 8
ROUNDS = 3
MAX_QUERY_LENGTH = 1000
Mode = Literal["lexical", "dense", "hybrid"]
KEY = re.compile(r"[a-z0-9-]{1,80}")


@dataclass(frozen=True)
class Guide:
    id: str
    scope: str
    title: str
    source: str
    text: str


@dataclass(frozen=True)
class Case:
    id: str
    query: str
    expected: list[str]
    scope: str | None = None


def read_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data or any(not isinstance(row, dict) for row in data):
        raise ValueError(f"{path.name}: ожидался непустой список записей JSON")
    return data


def load_guides() -> list[Guide]:
    guides = [Guide(**data) for data in read_json(FOLDER / "guides.json")]
    seen = set()
    for item in guides:
        if not all(isinstance(field, str) and field.strip() for field in asdict(item).values()):
            raise ValueError("У каждой записи должны быть заполнены все поля")
        url = urlparse(item.source)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(f"Некорректная ссылка у источника {item.id}")
        if item.id in seen or not KEY.fullmatch(item.id) or not KEY.fullmatch(item.scope):
            raise ValueError(f"Некорректный или повторяющийся ключ источника {item.id}")
        seen.add(item.id)
    return guides


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split())


class KnowledgeIndex:
    def __init__(self, guides: list[Guide], cache: Path) -> None:
        self.dense = TextEmbedding(DENSE_MODEL, cache_dir=str(cache), threads=2)
        self.sparse = SparseTextEmbedding(SPARSE_MODEL, cache_dir=str(cache), language="russian")
        self.client = QdrantClient(":memory:")
        self.client.create_collection(
            collection_name=COLLECTION,
            vectors_config={"dense": models.VectorParams(size=DIMENSIONS, distance=models.Distance.COSINE)},
            sparse_vectors_config={"lexical": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )

        documents = [normalize(f"{guide.title}\n{guide.text}") for guide in guides]
        dense_vectors = self.dense.passage_embed(documents)
        lexical_vectors = self.sparse.passage_embed(documents)
        points = [
            models.PointStruct(
                id=number,
                vector={"dense": dense.tolist(), "lexical": models.SparseVector(**lexical.as_object())},
                payload=asdict(guide),
            )
            for number, (guide, dense, lexical) in enumerate(
                zip(guides, dense_vectors, lexical_vectors, strict=True)
            )
        ]
        self.client.upsert(COLLECTION, points)

    def search(self, query: str, mode: Mode = "hybrid", scope: str | None = None) -> list[Guide]:
        if not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise ValueError(f"Запрос должен содержать от 1 до {MAX_QUERY_LENGTH} символов")
        if mode not in MODES:
            raise ValueError("Неизвестный режим поиска")
        if scope is not None and not KEY.fullmatch(scope):
            raise ValueError("Ключ темы должен содержать латинские буквы, цифры или дефисы")

        query = normalize(query)
        query_filter = None
        if scope is not None:
            query_filter = models.Filter(
                must=[models.FieldCondition(key="scope", match=models.MatchValue(value=scope))]
            )
        prefetch = []
        if mode != "lexical":
            vector = next(self.dense.query_embed(query)).tolist()
            prefetch.append(models.Prefetch(query=vector, using="dense", limit=PREFETCH_K))
        if mode != "dense":
            vector = next(self.sparse.query_embed(query))
            if len(vector.indices):
                lexical = models.SparseVector(**vector.as_object())
                prefetch.append(models.Prefetch(query=lexical, using="lexical", limit=PREFETCH_K))

        if not prefetch:
            return []
        if len(prefetch) == 1:
            [candidate] = prefetch
            result = self.client.query_points(
                COLLECTION, query=candidate.query, using=candidate.using,
                query_filter=query_filter, limit=TOP_K,
            )
        else:
            result = self.client.query_points(
                COLLECTION, query=models.FusionQuery(fusion=models.Fusion.RRF), prefetch=prefetch,
                query_filter=query_filter, limit=TOP_K,
            )
        return [Guide(**point.payload) for point in result.points]


def benchmark(index: KnowledgeIndex, guides: list[Guide], cases_path: Path) -> dict:
    cases = [Case(**row) for row in read_json(cases_path)]
    guide_ids = {guide.id for guide in guides}
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Повторяются идентификаторы проверочных запросов")
    for case in cases:
        if not isinstance(case.expected, list) or not set(case.expected) <= guide_ids:
            raise ValueError(f"{case.id}: указан неизвестный источник")

    report = {}
    for mode in MODES:
        index.search(cases[0].query, mode, cases[0].scope)
        latencies = []
        misses = []
        top_one = top_three = empty = 0
        for repeat in range(ROUNDS):
            for case in cases:
                started = perf_counter()
                found = [guide.id for guide in index.search(case.query, mode, case.scope)]
                latencies.append((perf_counter() - started) * 1000)
                if repeat:
                    continue
                if not case.expected:
                    empty += not found
                    continue
                exact = bool(found and found[0] in case.expected)
                top_one += exact
                top_three += bool(set(found).intersection(case.expected))
                if not exact:
                    misses.append({"case": case.id, "first": found[0] if found else None})
        report[mode] = {
            "hit_at_1": top_one, "hit_at_3": top_three, "correct_empty": empty,
            "median_ms": round(statistics.median(latencies), 2),
            "p95_ms": round(sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1], 2),
            "miss_at_1": misses,
        }
    return {
        "guides": len(guides),
        "answerable": sum(bool(case.expected) for case in cases),
        "empty_expected": sum(not case.expected for case in cases),
        "rounds": ROUNDS,
        "results": report,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск по проверенным источникам об экипировке")
    parser.add_argument("--cache", required=True, type=Path, help="Папка моделей FastEmbed")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mode", choices=MODES, default="hybrid")
    search.add_argument("--scope")
    measure = commands.add_parser("benchmark")
    measure.add_argument("--cases", type=Path, default=FOLDER / "cases.json")
    args = parser.parse_args()

    guides = load_guides()
    index = KnowledgeIndex(guides, args.cache)
    try:
        if args.command == "benchmark":
            result = benchmark(index, guides, args.cases)
        else:
            result = [asdict(guide) for guide in index.search(args.query, args.mode, args.scope)]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except ValueError as error:
        parser.error(str(error))
    finally:
        index.client.close()


if __name__ == "__main__":
    main()
