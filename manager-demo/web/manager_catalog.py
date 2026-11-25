"""Отображение вариантов для внутреннего каталога и резервной выгрузки."""
import json
import re
from decimal import Decimal, InvalidOperation

from archive_catalog import SIZES, Catalog, words

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
WEIGHT_ONLY = re.compile(r"^(?:а\s+)?(?:если\s+)?(?:вес\s+)?(\d{1,2}\s*(?:oz|унци\w*))"
                         r"(?:[, ]+все остальное как раньше)?[\s.!?]*$", re.I)


def catalog_followup(question):
    phrase = question.casefold().replace("ё", "е")
    return (bool(POSITION.search(phrase)) and not re.search(r"\bзаказ\w*\b", phrase)
            or bool(CITY_ONLY.fullmatch(phrase) or WEIGHT_ONLY.fullmatch(phrase)))


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
        phrase = question.casefold().replace("ё", "е")
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
            if self.criteria["product"]:
                if warehouse:
                    return self.search({**self.criteria, "warehouse": warehouse})
                if weight := WEIGHT_ONLY.fullmatch(phrase):
                    variant = self.criteria["variant"]
                    if re.fullmatch(r"[0-9a-f-]{36}", variant):
                        variant = next((row["вариант"] for row in self.rows if row["id"] == variant), "")
                    return self.search({**self.criteria, "variant": f"{SIZES.sub(' ', variant)} {weight[1]}".strip()})

        def keep_explicit_terms(messages):
            raw = parse(messages)
            socks = OXXFIRE_SOCKS.search(question)
            rashguards = VENUM_RASHGUARDS.fullmatch(question)
            if not (CAPA.search(question) or BOYBO.search(question) or socks or rashguards):
                return raw
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or not isinstance(parsed.get("product"), str):
                    return raw
                product = search_terms(parsed["product"])
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
                f"{self.archive_stock([idx], [city] if city else self.stores)}.")

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
                         f"{self.archive_stock(indices, stores)}.")
        return "\n".join([*lines, self.menu_hint()])

    def variant_summary(self, indices):
        return (self.label(indices[0]) if len(indices) == 1
                else f"характеристик: {len(indices)}")

    def menu_hint(self):
        if self.offset + 8 < len(self.options):
            return "Напишите номер позиции или «ещё»."
        return "Напишите номер позиции, чтобы уточнить вариант."
