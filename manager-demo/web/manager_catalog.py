"""Отображение вариантов для внутреннего каталога и резервной выгрузки."""
import json
import re
from decimal import Decimal, InvalidOperation

from archive_catalog import COLORS, Catalog, words
from manager_knowledge import OZ_RANGE, OZ_SIZE, expand_terms, normalize_ounces, normalized

CAPA = re.compile(r"\b(?:кап(?:а|ы|у|е|ой|ою|ам|ами|ах)?|защит[ау]\s+(?:для\s+)?зубов)\b", re.I)
BOYBO = re.compile(r"\b(?:boy[ -]?bo|бой[ -]?бо)\b", re.I)
OXXFIRE_SOCKS = re.compile(r"(?=.*\boxxfire\b)(?=.*\bноск\w*\b)", re.I)
VENUM_RASHGUARDS = re.compile(r"(?=.*\bvenum\b)(?=.*\bрашгард\w*\b)(?!.*\bкомплект\w*\b).+", re.I)
TOPIC_RESET = re.compile(r"^\s*забудь(?:те)? про (?:эти|эту|этот) "
                         r"(?:перчатки|капу|капы|товар|модель|экипировку|носки)\s*[.!]\s*", re.I)
POSITION = re.compile(r"\b(?:позици\w*|строк\w*|вариант\w*)\s*,?\s*(?:[№#]\s*)?(\d{1,3})\b|"
                      r"(?<!\w)[№#]\s*(\d{1,3})\b", re.I)
PRODUCT_POSITION = re.compile(r"\b(?:позици\w*|строк\w*)\b", re.I)
CITY_ONLY = re.compile(r"^(?:а\s+)?(?:только\s+)?(?:в|по)\s+(симферопол\w*|севастопол\w*)[\s.!?]*$", re.I)
CITY_NAMES = re.compile(r"\b(симферопол\w*|севастопол\w*)\b", re.I)
OZ_SHORT = re.compile(r"(?<![\w-])(6|8|10|12|14|16|18|20|22|24)-?(?:к[аиу]|ках|ок|шк[аиу]|s)(?![\w-])")
BRAND_LABEL = re.compile(r"\b(?:бренд\w*|марк[аиуеой]*)\s+", re.I)
WEIGHT_WORDS = re.compile(
    r"а|и|но|если|тогда|на|в|с|по|про|давай|покаж\w*|проверь\w*|только|"
    r"сколько|цен[ауыеи]|стоит|стоят|стоимост\w*|почем|остат\w*|свобод\w*|штук\w*|есть|"
    r"наличи\w*|како\w*|эт\w*|так\w*|те|же|сам\w*|вес\w*|перчат\w*|"
    r"из|буд\w*|все|остальн\w*|как|раньше|оставь|пожалуйста|плиз|сейчас|там"
)


def weight_words_only(phrase):
    remainder = CITY_NAMES.sub(" ", COLORS.sub(" ", phrase))
    return all(WEIGHT_WORDS.fullmatch(word) for word in re.findall(r"[\w-]+", remainder))


def glove_change(question):
    """A weight-only follow-up can carry a color, city and price or stock intent."""
    phrase = OZ_SHORT.sub(r"\1 oz", normalized(normalize_ounces(question)))
    weights = OZ_SIZE.findall(phrase)
    if len(weights) == 1 and not OZ_RANGE.search(phrase) and weight_words_only(OZ_SIZE.sub(" ", phrase)):
        return weights[0], " ".join(match[0] for match in COLORS.finditer(phrase))
    return None


def catalog_followup(question):
    phrase = normalized(question)
    expanded = expand_terms(phrase)
    return (bool(POSITION.search(phrase)) and not re.search(r"\bзаказ\w*\b", phrase)
            or bool(CITY_ONLY.fullmatch(expanded) or glove_change(expanded))
            or bool(OZ_RANGE.search(expanded) and weight_words_only(OZ_RANGE.sub(" ", expanded))))


def search_terms(text):
    return BOYBO.sub("BoyBo", CAPA.sub("капа", text))


def display(value):
    plain = " ".join(value.split())
    return re.sub(r"([\\`*_[\]<>!|~])", r"\\\1", plain)


def units(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Количество должно быть конечным")
    return format(max(number, Decimal(0)).normalize(), "f")


class ManagerCatalog(Catalog):
    def __init__(self, data):
        super().__init__(data)
        self.product_menu = None

    def reply(self, question, parse):
        question = expand_terms(question)
        phrase = normalized(question)
        city = CITY_NAMES.search(phrase)
        warehouse = next((store for store in self.stores
                          if city and store.casefold().startswith(city[1][:9])), None)
        if catalog_followup(phrase):
            if position := POSITION.search(phrase):
                if self.product_menu and PRODUCT_POSITION.search(phrase):
                    options, criteria = self.product_menu
                    self.options, self.criteria = options, criteria.copy()
                if warehouse:
                    self.criteria["warehouse"] = warehouse
                return super().reply("№" + (position[1] or position[2]), parse)
            product = self.criteria["product"]
            if change := glove_change(phrase):
                weight, color = change
                title = self.products.get(product, {}).get("товар", product)
                variant = self.criteria["variant"]
                if not product or ("перчат" not in title.casefold() and not OZ_SIZE.search(normalize_ounces(variant))):
                    if not re.search(r"\bперчат\w*\b", phrase):
                        return f"Какие перчатки на {weight} проверить? Назовите модель или марку."
                else:
                    if re.fullmatch(r"[0-9a-f-]{36}", variant):
                        variant = next((row["вариант"] for row in self.rows if row["id"] == variant), "")
                    attrs = OZ_SIZE.sub(" ", normalize_ounces(variant))
                    if color:
                        attrs = COLORS.sub(" ", attrs)
                    attrs = " ".join([attrs.strip(" ,"), weight, color]).strip()
                    return self.search({**self.criteria, "variant": attrs,
                                        "warehouse": warehouse or self.criteria["warehouse"]})
            if product and warehouse and CITY_ONLY.fullmatch(phrase):
                return self.search({**self.criteria, "warehouse": warehouse})

        if OZ_RANGE.search(phrase) or len(OZ_SIZE.findall(phrase)) > 1:
            return "Какой вес перчаток проверить первым? Укажите один вариант в унциях."

        def keep_explicit_terms(messages):
            raw = parse(messages)
            socks = OXXFIRE_SOCKS.search(question)
            rashguards = VENUM_RASHGUARDS.fullmatch(question)
            brand_label = BRAND_LABEL.search(question)
            if not (CAPA.search(question) or BOYBO.search(question) or socks or rashguards or brand_label):
                return raw
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or not isinstance(parsed.get("product"), str):
                    return raw
                product = search_terms(parsed["product"])
                variant = parsed.get("variant")
                if brand_label and isinstance(variant, str) and BRAND_LABEL.match(variant):
                    brand = BRAND_LABEL.sub("", variant, count=1).strip(" «»\"")
                    named = question[brand_label.end():].strip(" «»\"")
                    if brand and re.match(re.escape(brand) + r"(?![\w-])", named, re.I):
                        if not words(brand) <= words(product):
                            product = f"{product} {brand}".strip()
                        parsed["variant"] = ""
                if socks:
                    product = "носки OxxFire"
                if rashguards:
                    product = "рашгарды Venum"
                    parsed["variant"] = ""
                if CAPA.search(question) and "капа" not in words(product):
                    # Модель может оставить только бренд или вернуть предыдущую категорию.
                    # Оставляем термины, которые действительно названы в текущем вопросе.
                    product = " ".join(sorted(words(product) & words(search_terms(question))))
                    product = ("капа " + product).strip()
                if BOYBO.search(question) and "boybo" not in words(product):
                    product = (product + " BoyBo").strip()
                parsed["product"] = product
                return json.dumps(parsed, ensure_ascii=False)
            except ValueError:
                return raw

        return super().reply(question, keep_explicit_terms)

    def matching_rows(self, product, variant):
        codes, matches = super().matching_rows(search_terms(product), variant)
        if product == "рашгарды Venum":
            codes = [code for code in codes if "комплект" not in words(self.products[code]["товар"])]
            allowed = set(codes)
            matches = [i for i in matches if self.rows[i]["код"] in allowed]
        return codes, matches

    def label(self, idx):
        label = self.rows[idx]["вариант"]
        return display(label) if label and not label.startswith("Характеристика без") else "Без уточнения"

    def visible(self):
        for number, (kind, key) in enumerate(self.options[self.offset:self.offset + 8], self.offset + 1):
            if kind == "product":
                variant = self.criteria["variant"]
                attrs = words(variant, variant=True)
                indices = [i for i in self.by_product[key]
                           if attrs <= self.variant_words[i] or variant == self.rows[i]["id"]]
                yield number, indices
            else:
                yield number, [key]

    def menu(self):
        # Используется при восстановлении истории. Живые данные читаются только для итоговой страницы.
        lines = [f"Нашёл {len(self.options)} позиций."]
        for number, indices in self.visible():
            title = display(self.rows[indices[0]]["товар"])
            lines.append(f"№{number} {title}")
        if self.offset + 8 < len(self.options):
            lines.append("Могу показать ещё варианты.")
        return "\n".join(lines)

    def search(self, criteria):
        result = super().search(criteria)
        if self.options and self.options[0][0] == "product":
            self.product_menu = (self.options, self.criteria.copy())
        return (result.replace(" в выгрузке", " в каталоге")
                .replace(", марку или артикул", " или марку")
                .replace("название, артикул или код из 1С", "название или марку")
                .replace("название или код товара", "название товара"))

    def archive_stock(self, indices, stores):
        info = []
        for name in stores:
            amounts = []
            for idx in indices:
                value = self.rows[idx].get("склады", {}).get(name, {}).get("свободно")
                if value is not None:
                    try:
                        amounts.append(Decimal(units(value)))
                    except (InvalidOperation, ValueError):
                        pass
            if amounts and sum(amounts) > 0 and len(amounts) < len(indices):
                detail = f"от {units(sum(amounts))} шт."
            elif amounts and len(amounts) == len(indices):
                detail = f"{units(sum(amounts))} шт."
            else:
                detail = "нет данных"
            info.append(f"{display(name)} — {detail}")
        return "; ".join(info)

    def show(self, idx, city):
        row = self.rows[idx]
        return (f"{display(row['товар'])} · {self.label(idx)}\n"
                f"{self.archive_stock([idx], [city] if city else self.stores).rstrip('.')}.")

    def archive_answer(self, turns, parse):
        for question in turns:
            result = self.reply(question, parse)
        if not self.options or result != self.menu():
            return result
        lines = [result.splitlines()[0]]
        city = self.criteria["warehouse"]
        stores = [city] if city else self.stores
        for number, indices in self.visible():
            row = self.rows[indices[0]]
            lines.append(f"№{number} {display(row['товар'])} · {self.variant_summary(indices)}. "
                         f"{self.archive_stock(indices, stores).rstrip('.')}.")
        return "\n".join([*lines, self.menu_hint()])

    def variant_summary(self, indices):
        return (self.label(indices[0]) if len(indices) == 1
                else f"характеристик: {len(indices)}")

    def menu_hint(self):
        if self.offset + 8 < len(self.options):
            return "Напишите номер позиции или «ещё»."
        return "Напишите номер позиции, чтобы уточнить вариант."
