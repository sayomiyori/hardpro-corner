"""Size guidance from published variants and manufacturer measurement charts."""
import re

FIT_REQUEST = re.compile(
    r"\b(?:какой|какого|каким|подбери\w*|выбрать|определить)\s+(?:мне\s+)?размер\w*"
    r"|\bразмер\w*\s+(?:мне\s+)?подойд\w*|\bкак\s+(?:подобрать|выбрать)\s+размер\w*"
)
MEASUREMENTS = re.compile(
    r"\b(?P<field>рост|вес|(?:длина\s+)?стоп\w*|(?:обхват\s+)?кист\w*|"
    r"(?:обхват\s+)?груд\w*|(?:обхват\s+)?тали\w*|(?:обхват\s+)?бедр\w*)"
    r"\s*[:=—-]?\s*(?P<value>\d{1,3}(?:[.,]\d{1,2})?)(?:\s*(?:см|кг))?\b"
)
# Nike's foot-length row is in inches. These thresholds are rounded to 1 mm.
NIKE_EU = ((24.6,40),(24.9,40.5),(25.4,41),(25.9,42),(26.2,42.5),(26.7,43),
           (27.1,44),(27.5,44.5),(27.9,45),(28.4,45.5),(28.7,46),(29.2,47),(29.7,47.5))
FAIRTEX_OZ = ((8,36,51,14,16.5),(10,51,60,16.5,19),(12,60,75,19,21.6),
              (14,75,86,21.6,24),(16,86,120,21.6,24))
MEASUREMENT_BOUNDS = {"рост": (50,245), "вес": (15,220), "стоп": (14,36), "кист": (10,35),
                      "груд": (40,180), "тали": (40,180), "бедр": (40,190)}


def read_measurements(phrase):
    found = {}
    for match in MEASUREMENTS.finditer(phrase):
        key = next(stem for stem in MEASUREMENT_BOUNDS if stem in match["field"])
        value = float(match["value"].replace(",", "."))
        low, high = MEASUREMENT_BOUNDS[key]
        if low <= value <= high:
            found[key] = value
    return found


def matches_height(variant, height):
    return height.is_integer() and str(int(height)) in re.findall(r"\b(\d{3})\s*см\b", variant)


def guide(title, variants, measures, has_chart=False):
    """Return explanation and an optional catalogue token for the recommended size."""
    if re.search(r"боксерк|борцовк|ботинк|обувь", title):
        length = measures.get("стоп")
        if length is None:
            return "Измерьте обе стопы от пятки до самого длинного пальца в носках для тренировок. Напишите длину большей стопы в сантиметрах.", ""
        if "nike" in title and "machomai" in title and NIKE_EU[0][0] <= length <= NIKE_EU[-1][0]:
            size = next(size for bound,size in NIKE_EU if length <= bound)
            return (f"По мужской сетке Nike для стопы {length:g} см ориентир — EU {size:g}. "
                    "Сверьте ширину и посадку при примерке.", f"{size:g}")
        return "Длина стопы принята. Для точного размера нужна сетка этой модели. Можно сверить её на карточке товара или уточнить у менеджера.", ""
    if "перчат" in title and ("бокс" in title or "fairtex" in title):
        if "вес" not in measures or "кист" not in measures:
            return ("Напишите свой вес, обхват кисти по костяшкам без большого пальца и назначение перчаток: "
                    "мешок, тренировки или спарринг. Обхват помогает оценить посадку, oz — массу перчатки."), ""
        if "fairtex" in title and "bgv1" in title:
            sizes = [oz for oz, low, high, small, large in FAIRTEX_OZ
                     if low <= measures["вес"] <= high and small <= measures["кист"] <= large]
            if len(sizes) == 1:
                oz = sizes[0]
                return (f"По таблице Fairtex ваш вес и обхват совпадают с ориентиром {oz} oz. "
                        "Для спарринга уточните требования тренера.", f"{oz} oz")
        return ("Вес и обхват учёл. Посадку этой модели нужно сверить с таблицей производителя. "
                "Для спарринга согласуйте вес перчаток с тренером."), ""
    if re.search(r"рашгард|футболк|трико|шорт|штаны|тайтс|кимоно|форма|легинс|брюк", title):
        if "рост" not in measures:
            return ("Напишите рост, обхват груди и талии в сантиметрах. Для шорт и тайтсов полезен ещё обхват бёдер. "
                    "Я сверю мерки с доступной ростовкой модели."), ""
        height = measures["рост"]
        labels = sorted({int(m) for variant in variants for m in re.findall(r"\b(\d{3})\s*см\b", variant)})
        if height.is_integer() and int(height) in labels:
            return (f"В каталоге для этой модели есть маркировка на рост {int(height)} см. "
                    "Сверьте обхваты с таблицей модели, чтобы проверить посадку."), f"{int(height)} см"
        if labels and min(labels) <= height <= max(labels):
            nearby = " и ".join(str(size) for size in sorted(labels, key=lambda size: abs(size-height))[:2])
            return f"Ближайшие ростовки — {nearby} см. Для выбора между ними понадобятся обхваты и таблица модели.", ""
        if "груд" not in measures or "тали" not in measures:
            return "Рост учёл. Уточните обхват груди и талии в сантиметрах, для шорт и тайтсов добавьте обхват бёдер.", ""
        return ("Мерки учёл. Сверьте их с таблицей на карточке модели." if has_chart else
                "Мерки учёл. Для этой модели точный размер стоит подтвердить по фирменной таблице у менеджера."), ""
    return "Напишите, для кого подбираете товар, и нужную мерку из таблицы на карточке. Если таблицы нет, посадку подскажет менеджер.", ""
