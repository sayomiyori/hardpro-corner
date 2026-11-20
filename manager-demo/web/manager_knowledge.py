"""Search reviewed terminology and competition requirements for the manager."""
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from archive_catalog import words


RULES_INTENT = re.compile(
    r"\b(?:федераци\w*|сертификат\w*|турнир\w*|соревновани\w*|регламент\w*|допуск\w*|"
    r"лицензи\w*|одобрен\w*|омологаци\w*|фбр|фдр|фкр|вфс|фсбр|wkf)\b"
)
BLINCHIKI = re.compile(r"\bблинчик(?:и|ов|ами)\b")


def normalized(text):
    return text.casefold().replace("ё", "е")


@lru_cache(maxsize=1)
def knowledge():
    return json.loads(Path(__file__).with_name("manager-knowledge.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def glossary():
    aliases = {normalized(alias): item["term"] for item in knowledge()["terms"] for alias in item["aliases"]}
    patterns = sorted(aliases, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, patterns)) + r")(?!\w)"), aliases


def expand_terms(question):
    pattern, aliases = glossary()
    query = normalized(question)
    if re.search(r"\b(?:бокс\w*|меш[ко]\w*|груш\w*|перчат\w*)\b", query):
        query = BLINCHIKI.sub("снарядные перчатки", query)
    elif re.search(r"\b(?:штанг\w*|гриф\w*|диск\w*)\b", query):
        query = BLINCHIKI.sub("диски", query)
    return pattern.sub(lambda match: aliases[match[0]], query)


def competition_answer(question, previous=()):
    query = normalized(question)
    if not RULES_INTENT.search(query):
        return None
    rules = knowledge()["rules"]
    aliases = Counter(alias for rule in rules for alias in {frozenset(words(term)) for term in rule["aliases"]})
    for candidate in (query, *(normalized(text) for text in reversed(previous))):
        tokens = words(candidate)
        relevant = []
        for rule in rules:
            matches = [words(alias) for alias in rule["aliases"] if words(alias) <= tokens]
            if matches:
                score = max((aliases[frozenset(alias)] == 1, len(alias)) for alias in matches)
                relevant.append((score, rule))
        if relevant:
            break
    else:
        return "Уточните вид спорта, федерацию, дисциплину и турнир. У разных организаторов разные требования к форме."

    best = max(score for score, _ in relevant)
    matches = [rule for score, rule in relevant if score == best]
    if len(matches) > 1:
        return "Уточните дисциплину: " + ", ".join(rule["name"] for rule in matches) + "."
    rule = matches[0]
    lines = [
        f"{rule['name']}. {rule['summary']}",
        f"[{rule['document']}]({rule['url']}). Источник проверен {knowledge()['checked']}.",
    ]
    if "shop" in rule:
        lines.append(f"[Варианты для проверки в каталоге HARD-PRO]({rule['shop']}).")
    for model in knowledge()["models"]:
        if rule["name"] in model["rules"] and words(model["type"]) <= words(query):
            lines.append(f"В карточке HARD-PRO есть отметка о допуске модели [{model['name']}]({model['url']}).")
    lines.append("Для проверки конкретной модели нужны положение турнира, возрастная группа, маркировка и артикул изделия.")
    return "\n".join(lines)
