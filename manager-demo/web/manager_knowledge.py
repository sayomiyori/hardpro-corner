"""Search reviewed terminology and competition requirements for the manager."""
import json
import re
from collections import Counter
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from archive_catalog import words


RULES_INTENT = re.compile(
    r"\b(?:федераци\w*|сертификат\w*|турнир\w*|соревновани\w*|регламент\w*|допуск\w*|"
    r"лицензи\w*|одобрен\w*|омологаци\w*|фбр|фдр|фкр|вфс|фсбр|wkf)\b"
)
DEFINITION_REQUEST = re.compile(r"\b(?:(?:что|шо|чо) (?:такое|за|значит|означает)|"
                                r"расшифруй\w*|как расшифровывается)\s+")
DEFINITION_END = re.compile(r"^(.+?)\s*[,—-]?\s+это (?:что|шо|чо)(?: такое| вообще)?[\s.!?]*$")
HYPHENS = str.maketrans("‐‑–", "---")
OUNCE_NUMBERS = {
    "четыре": 4, "четырех": 4, "шесть": 6, "шести": 6, "восемь": 8, "восьми": 8,
    "десять": 10, "десяти": 10, "двенадцать": 12, "двенадцати": 12,
    "четырнадцать": 14, "четырнадцати": 14, "шестнадцать": 16, "шестнадцати": 16,
    "восемнадцать": 18, "восемнадцати": 18, "двадцать": 20, "двадцати": 20,
}
OUNCE_UNIT = r"(?:[oо0][zз]|унц(?:и[а-я]*|ов[а-я]*)?)"
OUNCE_NOTATION = re.compile(
    rf"(?<![\w-])(?P<weight>\d{{1,2}}(?:[.,]\d{{1,2}})?|{'|'.join(OUNCE_NUMBERS)})"
    rf"[\s-]*(?P<unit>{OUNCE_UNIT})(?![\w-])", re.I,
)
OZ_SIZE = re.compile(r"(?<![\w-])\d{1,2}(?:[.,]\d{1,2})?\s+oz\b", re.I)
OZ_RANGE = re.compile(rf"(?<![\w-])\d{{1,2}}(?:\s*{OUNCE_UNIT})?\s*"
                      rf"(?:[-/]|\bили\b|\bи\b|\bдо\b)\s*\d{{1,2}}\s*{OUNCE_UNIT}(?!\w)", re.I)
GRAM_CONVERSION = re.compile(r"\b(?:грамм\w*|г\b)\b")


def normalized(text):
    return text.casefold().replace("ё", "е").translate(HYPHENS)


def normalize_ounces(text):
    def replace(match):
        weight = match["weight"].casefold()
        return f"{OUNCE_NUMBERS.get(weight, weight.replace(',', '.'))} oz"

    return OUNCE_NOTATION.sub(replace, text.translate(HYPHENS))


@lru_cache(maxsize=1)
def knowledge():
    return json.loads(Path(__file__).with_name("manager-knowledge.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def terminology():
    return json.loads(Path(__file__).with_name("combat-terminology.json").read_text(encoding="utf-8"))


def whole_terms(terms):
    alternatives = "|".join(map(re.escape, sorted({normalized(term) for term in terms}, key=lambda s: (-len(s), s))))
    return re.compile(r"(?<![\w-])(?:" + alternatives + r")(?![\w-])")


@lru_cache(maxsize=1)
def glossary():
    entries = knowledge()["terms"] + [term for group in terminology()["groups"] for term in group["terms"]
                                    if term.get("search", True)]
    aliases = {normalized(alias): item["term"] for item in entries for alias in item["aliases"]}
    for item in entries:
        term = normalized(item["term"])
        if term in aliases:
            aliases[term] = item["term"]
    return whole_terms(aliases), aliases


@lru_cache(maxsize=1)
def context_patterns():
    return [(item, whole_terms(item["aliases"]),
             [(case, whole_terms(case["cues"])) for case in item["cases"]],
             whole_terms(item["ignore"]) if item.get("ignore") else None)
            for item in terminology()["ambiguities"]]


def resolve_context(question):
    query = normalized(question)
    for item, pattern, cases, ignored in context_patterns():
        probe = ignored.sub(lambda match: " " * len(match[0]), query) if ignored else query
        occurrences = list(pattern.finditer(probe))
        if not occurrences:
            continue
        matches = [case for case, cues in cases if cues.search(probe)]
        if not matches or (len(matches) > 1 and not item.get("allow_combined", False)):
            return query, item["question"]
        if len(matches) == 1 and "replacement" in matches[0]:
            for occurrence in reversed(occurrences):
                start, end = occurrence.span()
                query = query[:start] + matches[0]["replacement"] + query[end:]
    return query, None


def merge_clarification(turns):
    if len(turns) < 2 or not resolve_context(turns[-2])[1]:
        return turns
    reply = normalized(turns[-1]).strip(" ?!.,")
    reply = re.sub(r"^(?:для|на|по|в|это)\s+", "", reply)
    previous = normalized(turns[-2])
    for _, aliases, cases, _ in context_patterns():
        if aliases.search(previous) and any(cues.fullmatch(reply) for _, cues in cases):
            combined = f"{turns[-2]} {turns[-1]}"
            if not resolve_context(combined)[1]:
                return [*turns[:-2], combined]
    return turns


def expand_terms(question):
    query, clarification = resolve_context(question)
    if clarification:
        return query
    pattern, aliases = glossary()
    query = pattern.sub(lambda match: aliases[match[0]], query)
    query = normalize_ounces(query)
    # Keep the catalogue's numbered reference after NFKC converts № into "No".
    return re.sub(r"(?<!\w)no\s*(?=\d)", "№", query) if "№" in question else query


def definition_answer(question):
    query = normalized(question)
    weights = OZ_SIZE.findall(normalize_ounces(query))
    if (len(weights) == 1 and not OZ_RANGE.search(query) and GRAM_CONVERSION.search(query)
            and re.search(r"^(?:а\s+)?(?:сколько|перевед\w*|\d{1,2}\b)", query)):
        weight = weights[0].split()[0]
        grams = Decimal(weight) * Decimal("28.349523125")
        source = terminology()["sources"]["nist-ounce"]
        return (f"{weight.replace('.', ',')} oz — около {grams:.0f} г. "
                f"В карточках боксёрских перчаток такая маркировка относится к заявленной массе одной перчатки.\n"
                f"[{source['title']}]({source['url']})")
    request = DEFINITION_REQUEST.search(query)
    ending = DEFINITION_END.fullmatch(query)
    if not request and not ending:
        return None
    subject = (query[request.end():] if request else ending[1]).strip(" \t?!.«»\"")
    if (re.match(r"^(?:[oо][zз]|унци\w*|унцовк\w*)\s+(?:на|у|в)\s+.*перчат", subject)
            or OZ_SIZE.search(normalize_ounces(subject))):
        subject = "oz"
    if not subject:
        return None
    data = terminology()
    results = []
    disciplines = []
    for group in data["groups"]:
        if subject in {normalized(name) for name in [group["title"], *group["names"]]}:
            disciplines.append((group, {"term": group["title"], "note": group["scope"]}))
        for entry in group["terms"]:
            names = {normalized(name) for name in [entry["term"], *entry["aliases"]]}
            if subject in names:
                results.append((group, entry))
    lines = []
    for group, entry in (results or disciplines)[:3]:
        source = data["sources"][(entry.get("sources") or group["sources"])[0]]
        heading = f"{group['title']}. {entry['term']}" if results else group["title"]
        lines.append(f"{heading}. {entry['note']}\n[{source['title']}]({source['url']})")
    return "\n\n".join(lines) or None


def competition_answer(question, previous=()):
    raw = normalized(question)
    if not RULES_INTENT.search(raw):
        return None
    query, clarification = resolve_context(question)
    if clarification:
        return clarification
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
    category = "возрастная и весовая категории" if rule["name"].startswith("Бокс,") else "возрастная группа"
    lines.append(f"Для проверки конкретной модели нужны положение турнира, {category}, маркировка и артикул изделия.")
    return "\n".join(lines)
