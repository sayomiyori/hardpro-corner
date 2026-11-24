"""Отображение вариантов для внутреннего каталога и резервной выгрузки."""
import json
import re
from decimal import Decimal, InvalidOperation

from archive_catalog import Catalog, words

CAPA = re.compile(r"\b(?:кап(?:а|ы|у|е|ой|ою|ам|ами|ах)?|защит[ау]\s+(?:для\s+)?зубов)\b", re.I)
BOYBO = re.compile(r"\b(?:boy[ -]?bo|бой[ -]?бо)\b", re.I)


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
    def reply(self, question, parse):
        def keep_explicit_terms(messages):
            raw = parse(messages)
            if not (CAPA.search(question) or BOYBO.search(question)):
                return raw
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or not isinstance(parsed.get("product"), str):
                    return raw
                product = search_terms(parsed["product"])
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
        return super().matching_rows(search_terms(product), variant)

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
