"""Поиск в локальной выгрузке. Модель извлекает условия, приложение читает остаток."""
import json
import re
from collections import Counter

COLORS = re.compile(r"\b(красн|черн|бел|син|зелен|желт|розов|сер|фиолетов|оранжев|голуб|золот|коричнев)(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ую|юю|ых|их|ого|его|ом|ем|ым|им|ыми|ими)\b", re.I)
SIZES = re.compile(r"\b(?:[2-6]?[xх]{1,5}[slл]|xxs|xs|s|m|l|[мл](?=\s*(?:[,?.!]|$))|\d+(?:[.,]\d+)?\s*(?:oz|унци[яийю]|унций|см|сантиметр[а-я]*|кг))\b", re.I)
ALIASES = {"грин": "green", "хилл": "hill", "хил": "hill", "джуниор": "junior", "венум": "venum",
           "адидас": "adidas", "эверласт": "everlast", "твинс": "twins",
           "кап": "капа", "капы": "капа", "капу": "капа", "капой": "капа", "капами": "капа", "капах": "капа"}
COLOR_NAMES = {"red": "красный", "black": "черный", "white": "белый", "blue": "синий",
               "green": "зеленый", "yellow": "желтый", "grey": "серый", "gray": "серый",
               "pink": "розовый", "orange": "оранжевый", "purple": "фиолетовый", "brown": "коричневый"}

PARSE = """Ты разбираешь вопросы о каталоге спортивного магазина. Верни JSON с полями product, variant, warehouse.
Все значения строки. product — только название, категория, фирма, модель, артикул или код.
variant — только явно названные размер, вес, рост, цвет. Приведи слова к именительному падежу, размер к латинице,
известный бренд к написанию на товаре (Грин Хилл → Green Hill, Венум → Venum). Сохрани различия моделей и все числа.
Не дополняй неизвестные характеристики. Не заменяй конкретную модель похожей. Не включай слова «товар», «размер», «цвет».
В warehouse запиши явно названный город в именительном падеже, даже если склада там нет.
Для всех складов верни пустую строку. Для нового товара очисти предыдущие ограничения.
Для уточнения верни полные условия с учётом предыдущего выбора: сохрани товар, замени только уточнённый размер,
цвет или город. Фраза «а в другом городе?» без названия означает все склады.
Не отвечай на вопрос и не называй количество. Если определить товар нельзя, оставь product пустым.
"""

def words(text, *, variant=False):
    text = text.casefold().replace("ё", "е")
    result = []
    for word in re.findall(r"[2-6]x[ls]\b|\d+(?:[.,]\d+)?|[a-zа-я]+", text):
        word = ALIASES.get(word, word)
        if variant:
            word = COLOR_NAMES.get(word, word)
        word = word.replace(",", ".")
        if re.fullmatch(r"[2-6]x[ls]", word):
            word = "x" * int(word[0]) + word[-1]
        word = {"унций": "oz", "унции": "oz", "унция": "oz", "унцию": "oz", "унцийный": "oz",
                "сантиметров": "см", "сантиметра": "см", "сантиметр": "см",
                "м": "m", "л": "l", "хл": "xl", "унц": "oz"}.get(word, word)
        if len(word) >= 5 and re.fullmatch(r"[а-я]+", word):
            word = re.sub(r"(ыми|ими|ого|его|ому|ему|ами|ями|ые|ие|ый|ий|ой|ая|яя|ое|ее|ых|их|ую|юю|ки$|ов|ам|ах|ом|а|я|ы|и|у|е)$",
                          lambda m: "к" if m[0] == "ки" else "", word)
        if word not in {"для", "на", "по", "и", "в", "из", "с", "р", "размер", "цвет", "рост"}:
            result.append(word)
    return set(result)


COLOR_TERMS = set().union(*(words(color) for color in COLOR_NAMES.values()))


class Catalog:
    def __init__(self, data):
        self.stores = data["склады"]
        self.rows = data["товары"]
        self.products = {}
        self.product_words = {}
        self.by_product = {}
        self.variant_words = []
        for idx, row in enumerate(self.rows):
            code = row["код"]
            if code not in self.products:
                self.products[code] = row
                self.product_words[code] = words(" ".join([row["товар"], row["артикул"], code]))
                self.by_product[code] = []
            self.by_product[code].append(idx)
            attrs = words(row["вариант"], variant=True)
            title = words(row["товар"])
            if attrs & COLOR_TERMS:
                title -= COLOR_TERMS
            if any(x[:1].isdigit() or re.fullmatch(r"(?:x*[sl]|m)", x) for x in attrs):
                title = {x for x in title if not x[:1].isdigit() and not re.fullmatch(r"(?:x*[sl]|m)", x)}
            self.variant_words.append(attrs | title)
        self.count_names = Counter((r["код"], r["вариант"]) for r in self.rows)
        self.codes = {code.casefold(): code for code in self.products}
        self.criteria = dict(product="", variant="", warehouse="")
        self.options = []
        self.offset = 0

    def label(self, idx):
        row = self.rows[idx]
        name = row["вариант"]
        if self.count_names[row["код"], name] > 1 or name.startswith("Характеристика без"):
            return f"{name} [ID {row['id'] or '—'}]"
        return name

    def menu(self):
        end = min(self.offset + 8, len(self.options))
        lines = [f"Найдено {len(self.options)}. Уточните товар, размер или цвет, либо напишите № и номер строки."]
        for i in range(self.offset, end):
            kind, key = self.options[i]
            row = self.products[key] if kind == "product" else self.rows[key]
            detail = row["код"] if kind == "product" else self.label(key)
            lines.append(f"№{i + 1}  {row['товар']} — {detail}")
        if end < len(self.options):
            lines.append("Напишите «ещё» для продолжения.")
        return "\n".join(lines)

    def show(self, idx, city):
        row = self.rows[idx]
        lines = [f"{row['товар']} — {self.label(idx)}."]
        for warehouse in ([city] if city else self.stores):
            balance = row["склады"][warehouse]
            value = balance.get("свободно")
            lines.append(f"{warehouse}: свободно {value}." if value is not None
                         else f"{warehouse}: в выгрузке нет строки, количество неизвестно.")
        lines.append("Сегодняшнее наличие требует свежей сверки.")
        return "\n".join(lines)

    def matching_rows(self, product, variant):
        terms, attrs = words(product), words(variant, variant=True)
        if not terms:
            return [], []
        direct = [code for code, r in self.products.items()
                  if product.casefold() in {s.casefold() for s in (code, r["артикул"], r["товар"]) if s}]
        codes = direct or [code for code, tokens in self.product_words.items() if terms <= tokens]
        matches = [i for code in codes for i in self.by_product[code]
                   if attrs <= self.variant_words[i] or (variant and variant == self.rows[i]["id"])]
        return codes, matches

    def search(self, criteria):
        if not isinstance(criteria, dict) or set(criteria) != set(self.criteria) or not all(
                isinstance(x, str) and len(x) <= 500 for x in criteria.values()):
            raise ValueError("Некорректные условия поиска от модели")
        city = criteria["warehouse"].strip()
        if city:
            city = next((s for s in self.stores if words(s) == words(city)), None)
            if city is None:
                self.criteria = dict(criteria)
                self.options = []
                return "Такого склада в выгрузке нет. Доступны " + ", ".join(self.stores) + "."
        product, variant = criteria["product"].strip(), criteria["variant"].strip()
        if not product:
            return "Какой товар вас интересует? Назовите товар, марку или артикул."
        if not words(product):
            return "Уточните название или код товара."
        codes, matches = self.matching_rows(product, variant)
        # A failed size must never fall through to a similar size or turn into a zero.
        if not matches:
            self.options = []
            self.criteria = dict(product=product, variant=variant, warehouse=city)
            if codes:
                return "Для этого товара такая характеристика не найдена. Уточните размер или цвет."
            return "Совпадений в выгрузке нет. Уточните название, артикул или код из 1С."
        self.criteria = dict(product=product, variant=variant, warehouse=city)
        self.offset = 0
        codes = list(dict.fromkeys(self.rows[i]["код"] for i in matches))
        if len(codes) > 1:
            self.options = [("product", k) for k in codes]
        elif len(matches) > 1:
            self.criteria["product"] = codes[0]
            self.options = [("variant", i) for i in matches]
        else:
            self.options = []
            row = self.rows[matches[0]]
            self.criteria.update(product=row["код"], variant=row["вариант"])
            return self.show(matches[0], city)
        return self.menu()

    def reply(self, question, parse):
        short = question.strip().casefold().rstrip(".!").replace("ё", "е")
        if self.options and short in {"еще", "дальше"}:
            self.offset = self.offset + 8 if self.offset + 8 < len(self.options) else 0
            return self.menu()
        chosen = re.fullmatch(r"(?:№|#|номер\s+|вариант\s+№)\s*(\d+)", short)
        ordinal = {"первый": 1, "второй": 2, "третий": 3, "четвертый": 4,
                   "пятый": 5, "шестой": 6, "седьмой": 7, "восьмой": 8}.get(short.removesuffix(" вариант"))
        if chosen or (ordinal and self.options):
            n = int(chosen[1]) if chosen else ordinal
            if not (1 <= n <= len(self.options)):
                return "Уточните номер строки из последнего списка."
            kind, key = self.options[n - 1]
            if kind == "product":
                return self.search({**self.criteria, "product": key})
            row = self.rows[key]
            self.criteria.update(product=row["код"], variant=row["id"] or row["вариант"])
            self.options = []
            return self.show(key, self.criteria["warehouse"])
        request = json.dumps({"предыдущий поиск": self.criteria, "вопрос": question}, ensure_ascii=False)
        try:
            parsed = json.loads(parse([{"role":"system","content":PARSE}, {"role":"user","content":request}]))
        except json.JSONDecodeError:
            return "Не удалось разобрать запрос. Уточните название, размер и склад."
        if not isinstance(parsed, dict) or set(parsed) != set(self.criteria) or not all(isinstance(v,str) for v in parsed.values()):
            return "Не удалось разобрать запрос. Уточните название, размер и склад."
        # Keep explicitly spoken attributes when the model drops them or puts them in the product name.
        phrase = question.casefold().replace("ё", "е")
        colors = [m[0] for m in COLORS.finditer(phrase)]
        sizes = [m[0] for m in SIZES.finditer(phrase)]
        sizes.extend(re.findall(r"\b(?:размер[а-я]*|рост(?:ом|а)?)\s+(\d+(?:[.,]\d+)?)\b", phrase))
        if colors or sizes:
            variant = parsed["variant"].casefold().replace("ё", "е")
            product = parsed["product"]
            for pattern, anchors in ((COLORS, colors), (SIZES, sizes)):
                if anchors:
                    variant = pattern.sub(" ", variant)
                    product = pattern.sub(" ", product)
            parsed.update(product=product.strip(), variant=" ".join([variant, *colors, *sizes]).strip())
        codes = {self.codes[k] for k in re.findall(r"(?<!\w)\d+-\d+(?!\w)", phrase) if k in self.codes}
        if len(codes) == 1:
            parsed["product"] = codes.pop()
        if len(self.stores) > 1 and all(s.casefold().rstrip("ь") in phrase for s in self.stores):
            parsed["warehouse"] = ""
        if re.fullmatch(r"(?:оба|обоих|все|всех|по всем)\s+(?:склад\w*|магазин\w*)", parsed["warehouse"].casefold()):
            parsed["warehouse"] = ""
        return self.search(parsed)
