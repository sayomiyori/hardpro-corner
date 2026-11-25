"""Storefront answers from a public feed and fixed handlers."""
import re
import unicodedata

from archive_catalog import COLORS, SIZES, words
from manager_catalog import OZ_SHORT, POSITION
from manager_knowledge import OZ_RANGE, OZ_SIZE, expand_terms, resolve_context
from shop_fit import FIT_REQUEST, MEASUREMENTS, guide, read_measurements
from shop_knowledge import CATALOG_LINK, MANAGER_LINK, data, expand_shop_terms, manager_answer, policy_answer, scope_answer
from site_catalog import STORES

HANDOFF = "/shop?legal=seller"
HELP = "Помогу с ассортиментом, текущими ценами, комплектами и адресами HARD-PRO. Назовите товар или вид спорта."
PRIVATE = "Уберите личные данные и отправьте вопрос ещё раз. По конкретному заказу свяжитесь с магазином."
HEALTH = "Обратитесь к врачу за оценкой травмы или симптомов. Подбирать экипировку для лечения или возвращения к тренировкам по переписке небезопасно."
HEALTH_PATTERN = re.compile(
    r"\b(?:травм\w*|перелом\w*|вывих\w*|растяжени\w*|сотрясени\w*|диагноз\w*|операци\w*|реабилитаци\w*|"
    r"болит|болят|боль\b|болях|болями|обострен\w*|опух\w*|онемел\w*|ушиб\w*|грыж\w*|артрит\w*|сколиоз\w*|"
    r"беременн\w*|поврежден\w*|повредил\w*|сломал\w*|инсульт\w*|астм\w*|эпилепси\w*|симптом\w*|порва\w*|лечени\w*|лечит\w*|врач\w*|связк\w*)"
)
PERSONAL = re.compile(
    r"@|(?:\+?\d[\d .()\-]{8,}\d)|\b(?:снилс|паспорт\w*|cvv|cvc|мой телефон|мой адрес|меня зовут|мое фио|"
    r"мой заказ|номер (?:заказа|карты)|адрес доставки|позвоните мне|перезвоните|моя почта)\b"
)
INSTRUCTIONS = re.compile(
    r"\b(?:игнориру\w*|забудь|промпт\w*|prompt|инструкци\w*|jailbreak|админ\w*|парол\w*|токен\w*|"
    r"системн\w*\s+(?:сообщени\w*|правил\w*)|роль\s+(?:бота|системы)|выполни\s+команд\w*)\b"
)
CONTACT = re.compile(r"\b(?:контакт\w*|адрес\w*|телефон\w*|позвон\w*|номер магазина|где (?:ваш\w* |у вас )?(?:находит\w*|магазин\w*|вы\b)|"
                     r"как (?:найти|добраться|доехать)|(?:час\w*|режим|график) работы|самовывоз\w*|куда (?:подъехать|приехать))")
HISTORY = re.compile(r"истори\w*\s+цен|динамик\w*\s+цен|цен\w*\s+(?:раньше|была|менялась)|(?:была|менялась|измен\w*)\s+цен|(?:подешевел|подорожал)\w*")
KIT = re.compile(r"\b(?:комплект\w*|набор\w*|экипировк\w*|одежд\w*)\b")
BUILD_ORDER = re.compile(r"\b(?:собер\w*|собра\w*|подбер\w*)\s+(?:мне\s+)?заказ\b")
ADD_CART = re.compile(r"\b(?:добав\w*|полож\w*|закин\w*|кин\w*)\b.{0,120}\bв корзин\w*\b")
PRICE_REASON = re.compile(r"\b(?:почему|за что|откуда)\b.{0,120}\b(?:цен\w*|стои\w*|дорог\w*|дороже)\b")
CLOTHING_GROUP = re.compile(r"\b(?P<child>детск\w*|ребен\w*|мальчик\w*|девочк\w*)"
                            r"|\b(?P<women>женск\w*|женщин\w*)|\b(?P<men>мужск\w*|мужчин\w*)")
BANDAGE_LENGTH = re.compile(r"\b\d(?:[.,]\d+)?\s*(?:м|метр\w*)\b")
ITEMS = {
    "Снарядные перчатки": r"снаряд\w*\s+перчат\w*|перчат\w*\s+снаряд\w*",
    "Тайские носки": r"тайск\w*\s+носк\w*|носк\w*\s+(?:для\s+)?(?:муай|тайск)|голеностопник\w*",
    "Тайские лапы": r"тайск\w*\s+(?:лап\w*|п[эа]д\w*)|лап\w*\s+для\s+муай",
    "Груша на растяжках": r"груш\w*\s+на\s+(?:растяжк\w*|резинк\w*)",
    "Боксёрки": r"\bбоксерк\w*|\bобувь\s+для\s+бокса",
    "Ботинки самбо": r"\b(?:ботин\w*|обув\w*)\s+(?:для\s+)?самбо",
    "Кимоно для дзюдо": r"кимоно\s+для\s+дзюдо|дзюдог\w*",
    "Компрессионные штаны": r"компрессион\w*\s+штан\w*|\bтайтс\w*",
    "Тайские шорты": r"шорт\w*\s+для\s+тайск\w*\s+бокса|тайск\w*\s+шорт\w*",
    "Перчатки": r"\bперчат\w*",
    "Бинты": r"\bбинт\w*",
    "Капа": r"\bкап(?:а|ы|у|е|ой|ою|ам|ами|ах)?\b",
    "Шлем": r"\bшлем\w*",
    "Защита голени": r"защит\w* голен|щитк\w*|накладк\w* на голень",
    "Шорты": r"\bшорт\w*",
    "Рашгард": r"\bрашгард\w*",
    "Кимоно": r"\b(?:кимоно|ги)\b",
    "Пояс": r"\bпояс\w*",
    "Футболка": r"\bфутболк\w*",
    "Легинсы": r"\b(?:лосин|леггинс|легинс)\w*",
}
SPORTS = (
    (r"мма|\bmma\b", "ММА", ("Перчатки", "Рашгард", "Шорты", "Капа")),
    (r"муай|тайск\w*\s+бокс|кикбокс", "ударных тренировок", ("Перчатки", "Бинты", "Защита голени", "Капа")),
    (r"бокс", "бокса", ("Перчатки", "Бинты", "Капа")),
    (r"джиу|грэпплинг|грепплинг|\bbjj\b", "грэпплинга", ("Рашгард", "Шорты")),
    (r"фитнес|зал\b", "фитнеса", ("Футболка", "Шорты")),
)
FILLER = words("слушай есть какие какой можно купить в наличии сколько всего осталось покажи подскажи пожалуйста "
               "хочу ищу нужны нужна нужен нужныe найдите найди подберите подобрать предложи варианты магазин "
               "склад товар цену цена цены цен стоимость стоит размер вес цвет черный белый синий красный "
               "доступно севастополь симферополь севастополе симферополе руб рублей раньше история динамика "
               "менялась изменилась была изменение была подешевел подорожал еще данный а это него нее")
FILLER |= words("которые которых нужен именно проверь актуальная актуальное текущую сейчас назови узнаю список "
                "название модель бренда фирма для бокса боксерские тайбокс спортивные говорю пожалуйста "
                "дешевле продажа продаете продаже продадите только тоже же цветной остались осталось "
                "вот тогда таких такие такиеже те тех тем эти эту этом эту этих того том тот самом еще "
                "так так же всё все остальное как раньше вернемся вернёмся новое если теперь и размером "
                "весом наличии свободно свободный остаток остатки остатка цены сколько штук там город другой "
                "магазине склад складе стоит стоят почем в наличии подходящие номера номерами "
                "номер пожалуйста плиз которые именно того этого варианта их к выйдет обе списком "
                "раздел категория категории каталог ссылку ссылка карточка карточку дай скинь пришли отправь "
                "открыть открой той них ними этот это лучше глянь гляньте скиньте у вас")
CONTAINS_ARTICLE = re.compile(r"(?<![\w-])[a-zа-я0-9]+(?:-[a-zа-я0-9]+)+(?![\w-])", re.I)
BUDGET = re.compile(r"\b(?:не дороже|до|бюджет(?:ом)?)\s*(\d+(?:[ \u00a0]\d{3})*)\s*"
                    r"(тыс\w*|к\b)?\s*(?:₽|руб\w*)?(?!\d|\s*(?:oz|унц|см|кг)\b)")
QUANTITY = re.compile(r"\b(\d{1,3}|одну|две|два|три|четыре|пять)\s+(?:штук\w*|шт\.?|пар\w*)\b")
LEADING_QUANTITY = re.compile(
    rf"\b(?:нужно|нужны|возьму|куплю)\s+([1-9][0-9]{{0,2}})(?:\s+{COLORS.pattern})?"
    r"(?=\s+(?:перчат|шорт|бинт|кап|рашгард|трико|шлем|футболк)\w*\b)", re.I
)
STOREFRONT_INTENT = re.compile(r"\b(?:ищу|найди\w*|покажи\w*|подбер\w*|прода[её]\w*|"
                               r"магазин\w*|ассортимент\w*|достав\w*|в наличии|цена|стоит|стоимость|сколько стоят)\b")
STORE_LOCATION = re.compile(r"\b(?:офлайн[- ]магазин\w*|где вход|открыт\w*|выходно\w*|воскресень\w*)\b")
PUBLIC_HOTLINE = re.compile(r"\b(?:бесплатн\w* номер\w*|общ\w* номер\w*)\b")
NEGATIVE_ITEM = re.compile(r"\s+(?:не\s+(?!дороже\b|более\b|дешевле\b|менее\b)|без\s+)")
PART_OF_STORE = re.compile(r"\b(?:севастопол\w*|симферопол\w*)\b")
EXACT_ATTRIBUTES = re.compile(
    r"\b(?:(?:размер\w*|рост(?:ом|а)?)\s+(\d+(?:[.,]\d+)?)"
    r"|(?<![\w-])(\d+(?:[.,]\d+)?)\s+(?:размер\w*(?!\s+(?:[0-9]|[2-6]?[xх]*[mslмл]\b))"
    r"|р(?=[\s.,!?]|$)))\b"
)
FRACTIONAL_SHOE_SIZE = re.compile(r"(?<![\w.,])(?:3[0-9]|4[0-9]|50)[.,][05](?![\w.,])")
SHOE_PRODUCT = re.compile(r"\b(?:боксерк\w*|борцовк\w*|ботин\w*|обувь)\b")
FORTY_SHOE_SIZE = re.compile(r"\bсороков(?:ой|ого|ому|ым|ом)\b")
PRICE_PER_PAIR = re.compile(r"\b(?:почем|сколько стоит)\s+(?:одна\s+)?пара\b")
SAMBO_SHOES = re.compile(r"\bсамбовк\w*[^?]*?\bтолько\s+(?:обувь|ботинки)\b")
LONG_SLEEVE = re.compile(r"\bдлинн\w*\s+рукав\w*\b")
SHIN_SLEEVE = re.compile(r"\bчул(?:ок|к\w*)\b")
PHOTO_QUERY = re.compile(r"\b(?:фото\w*|фотк\w*|картинк\w*|изображени\w*|как выгляд\w*)\b")
LINK_QUERY = re.compile(r"\b(?:ссылк\w*|карточк\w*)\b")
OFFER_QUERY = re.compile(r"\b(?:цен\w*|стоимост\w*|стоит|стоят|почем|сколько|наличи\w*|остат\w*|есть)\b")
SECTION_QUERY = re.compile(r"\b(?:раздел\w*|категори\w*)\b")
COLOR_EXCEPTIONS = {"син": "синий", "голуб": "голубой", "золот": "золотой"}
MISSING = "В опубликованном каталоге пока не нашёл таких товаров."


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split())


def blocked(text):
    phrase = normalize(text)
    if HEALTH_PATTERN.search(phrase):
        return HEALTH, []
    if PERSONAL.search(phrase):
        return PRIVATE, [{"label":"Связаться с магазином", "href":HANDOFF}]
    if INSTRUCTIONS.search(phrase) or any(unicodedata.category(c)=="Cf" for c in text):
        return HELP, []
    return None


def rub(cents):
    rubles, kopecks = divmod(cents, 100)
    value = f"{rubles:,}".replace(",", " ")
    return f"{value},{kopecks:02d} ₽" if kopecks else f"{value} ₽"


def link(row, exact=False, city=None):
    label = row["товар"] + (f" · {row['вариант']}" if exact else "")
    href = "/shop/product?id=" + row["код"][1:]
    if exact and row["id"]:
        href += "&sku=" + row["id"]
    photo = (row.get("photo") if exact else "") or row.get("product_photo", "")
    result = {"label":label, "href":href, "title":row["товар"]}
    if city:
        result["city"] = city
    if exact and row["id"]:
        result["variant"] = row["вариант"]
    if photo:
        result["photo"] = photo
        if exact and not row.get("photo"):
            result["photo_note"] = "Фото модели"
    return result


def city_in(phrase):
    selected = [city for city in STORES if city.casefold().rstrip("ь") in phrase]
    return selected[0] if len(selected)==1 else None


def budget_limit(phrase):
    match = BUDGET.search(phrase)
    return int(re.sub(r"\s", "", match[1])) * (100_000 if match[2] else 100) if match else None


def kit_sizes(phrase):
    phrase = BANDAGE_LENGTH.sub(" ", phrase)
    labels = [m[0] for m in SIZES.finditer(phrase) if not re.search(r"oz|унц|кг",m[0])]
    labels += re.findall(r"\bразмер\w*\s+(\d+(?:[.,]\d+)?)(?![\d.,]|\s*(?:oz|унц|кг))",phrase)
    return labels


def attributes(text):
    size_text = OZ_SIZE.sub(" ", text)
    sizes = [m[0] for m in SIZES.finditer(size_text)]
    sizes.extend(left or right for left, right in EXACT_ATTRIBUTES.findall(size_text))
    colors = [COLOR_EXCEPTIONS.get(stem, stem + "ый") for stem in COLORS.findall(text)]
    return {kind: " ".join(values) for kind, values in {
        "color": colors, "weight": OZ_SIZE.findall(text), "size": sizes,
    }.items() if values}


class ShopDialog:
    def __init__(self, catalog, history=None):
        self.cat = catalog
        self.history = history
        self.selected = []
        self.options = []
        self.page = 0
        self.city = None
        self.filters = {}
        self.compare = False
        self.budget = None
        self.pending_kit = ""
        self.last_kit = ""
        self.last_quantity = None
        self.fit_code = None
        self.fit_pending = False
        self.fit_measures = {}
        self.display = None
        self.cart = None
        self.page_variant = None

    def start_from_product_page(self, context):
        code = f'P{context["product"]}'
        if code not in self.cat.by_product:
            return
        self.selected = [code]
        self.city = context.get("city")
        if sku := context.get("sku"):
            self.page_variant = next((self.cat.rows[i] for i in self.cat.by_product[code]
                                      if self.cat.rows[i]["id"] == str(sku)), None)
            if self.page_variant is not None:
                self.filters = attributes(normalize(self.page_variant["вариант"]))

    @property
    def attrs(self):
        return words(" ".join(self.filters.values()), variant=True)

    def stores(self, city, *, hotline=False, fallback_city=False):
        headline = []
        links = []
        if hotline:
            phone = data()["store"]["hotline"]
            headline.append("Общий бесплатный номер по России — 8 800 350-54-14.")
            links.append({"label":"Общий телефон HARD-PRO","href":"tel:" + phone})
        if fallback_city:
            headline.append("Офлайн-магазины HARD-PRO есть только в Севастополе и Симферополе. Для других городов доставку согласует менеджер.")
        if hotline and not city and not fallback_city:
            return " ".join(headline), links
        rows = [c for c in self.cat.contacts if not city or city==c["city"]]
        if not rows:
            return "Адреса и телефоны опубликованы на странице контактов.", [{"label":"Контакты магазинов","href":HANDOFF}]
        for row in rows:
            links.extend((
                {"label":f'{row["city"]} · адрес', "href":HANDOFF + "#" + row["key"]},
                {"label":f'Позвонить {row["phone"]}', "href":"tel:" + row["phone"]},
            ))
        details = data()["store"].get("entrances", {})
        return "\n".join(headline + [
            " ".join(part for part in (f'{c["address"]}.', details.get(c["city"], ""),
                                       f'{c["hours"]}. Телефон {c["phone"]}.') if part)
            for c in rows
        ]), links

    def sections(self, phrase):
        kind = next(((name, pattern) for name, pattern in ITEMS.items() if re.search(pattern, phrase)), None)
        query = re.sub(kind[1], kind[0], phrase, count=1) if kind else phrase
        parts = words(SECTION_QUERY.sub(" ", query)) - FILLER
        options = [(category, name) for category, name in self.cat.sections
                   if not parts or parts <= words(name)]
        exact = [(category, name) for category, name in options if parts == words(name)]
        if exact:
            options = exact
        elif not options and kind and parts == words(kind[0]):
            options = [(category, name) for category, name in self.cat.sections if re.search(kind[1], normalize(name))]
        if not options:
            return "Такого раздела не нашёл. Все категории собраны в каталоге.", [CATALOG_LINK]
        options.sort(key=lambda item: (len(item[1]), item[0]))
        links = [{"label": name, "href": f"/shop?category={category}"} for category, name in options[:6]]
        if len(options) > len(links):
            links.append(CATALOG_LINK)
        return ("Вот нужный раздел." if len(links) == 1 else "Подобрал разделы каталога."), links

    def propose_cart(self, rows):
        cities = [city for city in STORES if all(row["склады"][city] >= qty for row,qty in rows)]
        city = self.city if self.city in cities else cities[0] if self.city is None and len(cities) == 1 else None
        if city:
            self.cart = {"city": city, "items": [
                {"product": int(row["код"][1:]), "sku": int(row["id"]), "qty": qty,
                 "price_cents": row["price_cents"]} for row,qty in rows
            ]}

    def fit(self, code, phrase):
        if self.fit_code != code:
            self.fit_measures = {}
        self.fit_code = code
        self.fit_pending = False
        self.fit_measures.update(read_measurements(phrase))
        product = self.cat.products[code]
        rows = [self.cat.rows[i] for i in self.cat.by_product[code] if self.cat.rows[i]["id"]]
        text,size = guide(normalize(product["товар"]), [row["вариант"] for row in rows],
                          self.fit_measures, getattr(self.cat,"details",{}).get(code,{}).get("size_chart",False))
        if not size:
            return f'{product["товар"]}. {text}', [link(product,city=self.city)]
        self.filters.pop("size",None)
        self.filters.pop("weight",None)
        self.filters["weight" if size.endswith(" oz") else "size"] = size
        result,links = self.choose([code])
        if self.display is not None:
            self.display = text + ("\n" + self.display if self.display else "")
        return text + "\n" + result, links

    def explain_price(self, codes, result, links):
        if len(codes) != 1:
            return result, links
        code = codes[0]
        details = getattr(self.cat,"details",{}).get(code,{})
        facts = [f"{label} — {details[key]}" for key,label in (("brand","бренд"),("material","материал")) if details.get(key)]
        product = self.cat.products[code]
        title = normalize(product["товар"])
        if "fairtex" in title and "bgv1" in title:
            facts.append("натуральная кожа и трёхслойный наполнитель по описанию Fairtex")
        text = ("Для сравнения с другими моделями можно учесть характеристики этой карточки — " + ", ".join(facts) + ". "
                if facts else "В карточке указана текущая розничная цена выбранного варианта. ")
        text += ("Себестоимость и наценку магазин не публикует. "
                 "Могу подобрать вариант дешевле, если укажете бюджет.")
        if self.display is not None:
            self.display = text + ("\n" + self.display if self.display else "")
        return text + "\n" + result, links

    def equipment(self, phrase):
        explicit = tuple(name for name in ("Перчатки","Бинты","Капа","Шлем","Защита голени","Шорты",
                                           "Рашгард","Кимоно","Пояс","Футболка","Легинсы")
                         if re.search(ITEMS[name], phrase))
        sport = next((sport for sport in SPORTS if re.search(sport[0], phrase)), None)
        slots = explicit if len(explicit)>1 or explicit and BUILD_ORDER.search(phrase) else (sport[2] if sport else ())
        if "одежд" in phrase and len(explicit)<2:
            wearable = tuple(slot for slot in slots if slot in {"Рашгард","Шорты","Кимоно","Футболка","Легинсы"})
            clothes = wearable or (("Футболка","Шорты") if sport else ())
            slots = tuple(dict.fromkeys((*slots,*clothes))) if "экипировк" in phrase else clothes
        if not slots:
            self.pending_kit = phrase
            return "Для какого спорта собрать комплект? Укажите нужные вещи, размеры или мерки и общий бюджет.", []
        self.pending_kit = ""
        self.last_kit = phrase
        self.options = []
        apparel = {"Рашгард","Шорты","Кимоно","Футболка","Легинсы"}
        size_labels = kit_sizes(phrase)
        if (height := read_measurements(phrase).get("рост")) and height.is_integer() and not size_labels:
            size_labels.append(f"{height:g} см")
        sizes = words(size_labels[0],variant=True) if len(set(size_labels))==1 else set()
        mentioned = sorted((found.start(), found.end(), slot) for slot in slots
                           if (found := re.search(ITEMS[slot], phrase)))
        specifics = {slot: phrase[end:mentioned[i+1][0] if i+1<len(mentioned) else len(phrase)].split(", ",1)[0]
                     for i,(_,end,slot) in enumerate(mentioned)}
        requested_group = CLOTHING_GROUP.search(phrase)
        group = requested_group.lastgroup if requested_group else None
        weight = words(attributes(phrase).get("weight",""),variant=True)
        ranked, missing, used = [], [], set()
        for slot in slots:
            best = {}
            needed = set()
            detail = specifics.get(slot,"")
            if slot in apparel or slot in {"Шлем","Защита голени","Пояс"}:
                local_sizes = kit_sizes(detail)
                if len(set(local_sizes))==1:
                    needed = words(local_sizes[0],variant=True)
                elif slot in apparel:
                    needed = sizes
            elif slot=="Перчатки":
                needed = weight
            elif slot=="Бинты" and (length := BANDAGE_LENGTH.search(detail)):
                needed = words(length[0],variant=True)
            amount = re.search(rf"\b(\d{{1,2}})\s*(?:шт\.?\s*|пар\w*\s*)?(?:{COLORS.pattern}\s+)?(?:{ITEMS[slot]})", phrase)
            color = re.search(rf"{COLORS.pattern}\s+(?:{ITEMS[slot]})",phrase) if BUILD_ORDER.search(phrase) else None
            quantity = max(1,int(amount[1])) if amount else 1
            exact = bool(needed)
            for row in self.cat.rows:
                stock = row["склады"]
                title = normalize(row["товар"])
                if (row["код"] in used or not row["id"] or not row["price_cents"]
                        or not re.search(ITEMS[slot], title)
                        or (exact and not needed<=words(row["вариант"],variant=True))
                        or (stock[self.city] if self.city else max(stock.values()))<quantity):
                    continue
                if color and not any(match[1] == color[1] for match in COLORS.finditer(normalize(row["вариант"]))):
                    continue
                row_group = CLOTHING_GROUP.search(title) if slot in apparel else None
                if ((group and row_group and row_group.lastgroup != group)
                        or (group=="child" and slot in apparel and not row_group and not sizes)):
                    continue
                if slot=="Перчатки" and sport:
                    if sport[1]=="ММА" and not re.search(r"\b(?:мма|mma|грэпплинг)\b", title):
                        continue
                    if sport[1]!="ММА" and not re.search(r"бокс|(?:\d+\s*oz)", title):
                        continue
                key = row["код"]
                if key not in best or row["price_cents"]<best[key]["price_cents"]:
                    best[key] = row
            if not best:
                missing.append(slot)
                continue
            choice = min(best.values(),key=lambda row:(
                bool(slot in apparel and not group and CLOTHING_GROUP.search(normalize(row["товар"]))),
                row["price_cents"],row["товар"],row["id"]
            ))
            used.add(choice["код"])
            ranked.append((slot,choice,exact,quantity))
        if not ranked:
            return "Сейчас не нашёл доступных позиций для этого комплекта. Уточните состав или магазин.", []
        place = f"Магазин · {self.city}" if self.city else "Подборка по двум магазинам"
        lines = [place]
        if not self.city:
            lines.append("Позиции могут быть в разных магазинах.")
        display = lines.copy()
        for slot,row,exact,quantity in ranked:
            variant = f' ({row["вариант"]})' if exact else ""
            price = rub(row["price_cents"])
            count = f" × {quantity}" if quantity != 1 else ""
            lines.append(f'{slot} · {row["товар"]}{variant}{count} · {price if exact else "от "+price}')
        summary_start = len(lines)
        total = sum(row["price_cents"]*qty for _,row,_,qty in ranked)
        confirmed = [(slot,row,exact or normalize(row["вариант"]) in {"стандарт","стандартный","стандарт, стандарт"},qty)
                     for slot,row,exact,qty in ranked]
        complete = not missing and all(exact for _,_,exact,_ in confirmed)
        lines.append(f"Сумма найденных позиций {rub(total)}." if complete else
                     f"Сумма найденных позиций от {rub(total)}. Цена зависит от варианта.")
        budget = budget_limit(phrase)
        if budget is not None:
            if total>budget:
                lines.append("Эта подборка выше указанного бюджета.")
            elif missing:
                lines.append(f"В бюджете остаётся {rub(budget-total)} на недостающие позиции.")
        if missing:
            lines.append("Не нашёл: " + ", ".join(missing) + ".")
            lines.append("Полный комплект сейчас недоступен. Наличие недостающих позиций можно уточнить у магазина.")
        if complete and (budget is None or total <= budget):
            self.propose_cart([(row,qty) for _,row,_,qty in ranked])
            if not self.cart and (BUILD_ORDER.search(phrase) or ADD_CART.search(phrase)):
                lines.append("Уточните магазин для добавления комплекта в корзину.")
        elif not missing and BUILD_ORDER.search(phrase):
            lines.append("Уточните размеры или вес, чтобы подтвердить варианты для корзины.")
        if ADD_CART.search(phrase) and not self.cart and not missing and not complete:
            lines.append("Подтвердите размеры или вес, чтобы добавить комплект в корзину.")
        cards = []
        for _,row,exact,quantity in (confirmed if complete else ranked):
            card = dict(link(row, exact, self.city), price=rub(row["price_cents"]) if exact else "от " + rub(row["price_cents"]))
            if quantity > 1:
                card["facts"] = [f"В комплекте · {quantity} шт."]
            cards.append(card)
        self.display = "\n".join(display + lines[summary_start:])
        return "\n".join(lines), cards + ([MANAGER_LINK] if missing else [])

    def active_variant(self, idx):
        row = self.cat.rows[idx]
        return (self.attrs <= self.cat.variant_words[idx]
                and (self.budget is None or 0 < row["price_cents"] <= self.budget))

    def menu(self):
        part = self.options[self.page:self.page+8]
        title = (f"Варианты по магазину «{self.city}». Напишите номер, чтобы уточнить один."
                 if self.city else "Выберите вариант по номеру или напишите нужный размер и цвет.")
        lines, links = [title], []
        for number, (exact, row) in enumerate(part,self.page+1):
            label = row["товар"] + (" · " + row["вариант"] if exact and row["id"] else "")
            card = dict(link(row, exact, self.city), label=f"№{number} {label}", position=number)
            links.append(card)
            variants = ([row] if exact else
                        [self.cat.rows[i] for i in self.cat.by_product[row["код"]]
                         if self.active_variant(i)])
            active = [variant for variant in variants if variant["id"]]
            if not active:
                lines.append(f"№{number} {label} · нет активных вариантов")
                card["facts"] = ["Пока без вариантов для покупки"]
                continue
            prices = sorted({variant["price_cents"] for variant in active if variant["price_cents"]})
            if len(prices) > 1:
                price = f"{rub(prices[0])} – {rub(prices[-1])}"
            elif prices:
                price = rub(prices[0])
            else:
                price = "цена не указана"
            facts = [f"{city} · {sum(variant['склады'][city] for variant in active)} шт."
                     for city in ([self.city] if self.city else STORES)]
            card.update(price=price, facts=facts)
            stock = "; ".join(facts)
            lines.append(f"№{number} {label} · {price}. {stock.rstrip('.')}.")
        if len(self.options)>8:
            lines.append(f"Показано {self.page+1}–{self.page+len(part)} из {len(self.options)}. Напишите «ещё» для продолжения.")
        self.display = "\n".join([title, lines[-1]] if len(self.options)>8 else [title])
        return "\n".join(lines), links

    def show(self, row, quantity=None):
        self.page_variant = None
        self.options = []
        self.selected = [row["код"]]
        self.last_kit = ""
        self.last_quantity = quantity
        self.filters = attributes(normalize(row["вариант"])) if row["id"] else {}
        if not row["id"]:
            return f'{row["товар"]}. У карточки пока нет активных вариантов для покупки.', [link(row, city=self.city)]
        card = link(row, True, self.city)
        lines = [f'{row["товар"]} · {row["вариант"]}']
        if row["price_cents"]:
            card["price"] = rub(row["price_cents"])
            lines.append("Цена сейчас " + card["price"] + ".")
            if row["old_price_cents"] > row["price_cents"]:
                card["old_price"] = rub(row["old_price_cents"])
                lines.append("На карточке зачёркнута цена " + card["old_price"] + ".")
        facts = []
        for city in [self.city] if self.city else STORES:
            lines.append(f'{city} — доступно {row["склады"][city]} шт.')
            quantity_on_hand = row["склады"][city]
            facts.append(f"{city} · {quantity_on_hand} шт." if quantity_on_hand else f"{city} · нет в наличии")
        card["facts"] = facts
        if row["price_cents"]:
            self.propose_cart([(row,quantity or 1)])
        details_start = len(lines)
        if quantity:
            available = row["склады"][self.city] if self.city else max(row["склады"].values())
            if available < quantity:
                lines.append(f"Для покупки {quantity} шт. в одном магазине не хватает остатка по этому варианту.")
            elif row["price_cents"]:
                lines.append(f"За {quantity} шт. — {rub(row['price_cents']*quantity)} по текущей цене.")
        if self.compare:
            points = self.history.recent(row["id"], row["код"][1:]) if self.history else []
            if points:
                lines.append("Наблюдения по этому варианту:")
                lines.extend(f"{date} · {rub(price)}" for date,price in points)
                delta = row["price_cents"] - points[0][1]
                if len(points)>1 or delta:
                    change = "выше на " + rub(delta) if delta>0 else "ниже на " + rub(-delta) if delta<0 else "совпадает с первой наблюдавшейся ценой"
                    lines.append("Текущая цена " + change + ".")
                else:
                    lines.append("Пока есть только первое наблюдение. Изменение цены подтвердить рано.")
            else:
                lines.append("Пока нет подтверждённой истории по этому варианту.")
        self.display = "\n".join(lines[details_start:])
        return "\n".join(lines), [card]

    def choose(self, codes, quantity=None):
        self.page_variant = None
        indices = [i for code in codes for i in self.cat.by_product[code]]
        rows = [self.cat.rows[i] for i in indices if self.active_variant(i)]
        if not rows:
            self.options = []
            self.selected = codes
            if self.budget is not None:
                return f"Вариантов до {rub(self.budget)} сейчас нет. Менеджер может помочь с подбором.", [MANAGER_LINK]
            if len(codes) == 1:
                product = self.cat.products[codes[0]]
                variants = dict.fromkeys(self.cat.rows[i]["вариант"] for i in indices if self.cat.rows[i]["id"])
                choices = ", ".join(list(variants)[:8])
                params = ", ".join(self.filters.values())
                detail = f'Запрошенный вариант ({params}) не найден.' if params else "Подходящий вариант не найден."
                text = f'{product["товар"]}. {detail}'
                if self.city:
                    text += f" Магазин — {self.city}."
                if choices:
                    text += f" Варианты на сайте — {choices}."
                return text, [link(product, city=self.city), MANAGER_LINK]
            return "Для этих моделей нужный размер или цвет пока не нашёл.", [MANAGER_LINK]
        self.selected = list(dict.fromkeys(row["код"] for row in rows))
        if len(rows)==1:
            return self.show(rows[0], quantity)
        if len(self.selected)>1 and len(rows)>len(self.selected):
            self.options = [(False,self.cat.products[code]) for code in self.selected]
        else:
            self.options = [(True,row) for row in rows]
        self.page = 0
        return self.menu()

    def answer(self, turns):
        result = (HELP, [])
        for question in turns:
            self.display = None
            self.cart = None
            restricted = scope_answer(normalize(question)) or blocked(question)
            if restricted:
                result = restricted
                continue
            question = SAMBO_SHOES.sub("ботинки самбо", question.casefold())
            question = expand_shop_terms(question)
            _, clarification = resolve_context(question)
            result = (clarification, []) if clarification else self.turn(expand_terms(question).casefold())
        return result

    def shoe_size_query(self, query):
        if FORTY_SHOE_SIZE.search(query) and (
            SHOE_PRODUCT.search(query)
            or any(SHOE_PRODUCT.search(normalize(self.cat.products[code]["товар"])) for code in self.selected)
        ):
            return FORTY_SHOE_SIZE.sub("размер 40", query)
        match = FRACTIONAL_SHOE_SIZE.search(query)
        if not match or EXACT_ATTRIBUTES.search(query) or SIZES.match(query[match.start():]):
            return query
        terms = words(PART_OF_STORE.sub(" ", FRACTIONAL_SHOE_SIZE.sub(" ", query, count=1))) - FILLER
        if terms and any(terms <= tokens and SHOE_PRODUCT.search(normalize(self.cat.products[code]["товар"]))
                         for code, tokens in self.cat.product_words.items()):
            return query[:match.start()] + "размер " + query[match.start():]
        return query

    def turn(self, phrase):
        if self.fit_code and re.fullmatch(r"\d{2}(?:[.,]\d{1,2})?\s*см[.!?]*",phrase) and (
            SHOE_PRODUCT.search(normalize(self.cat.products[self.fit_code]["товар"]))
        ):
            phrase = "стопа " + phrase
        city = city_in(phrase)
        cart_add = bool(ADD_CART.search(phrase))
        fit_request = bool(FIT_REQUEST.search(phrase)) or self.fit_pending
        price_reason = bool(PRICE_REASON.search(phrase))
        if handoff := manager_answer(phrase):
            return handoff
        if not cart_add and (policy := policy_answer(phrase)):
            if city and re.search(r"\b(?:самовывоз\w*|забрать\w*)\b", phrase) and not re.search(r"\bналичн\w*\b", phrase):
                contacts, links = self.stores(city)
                return policy[0] + "\n" + contacts, links
            return policy
        if SECTION_QUERY.search(phrase) and self.cat.sections:
            return self.sections(phrase)
        if CONTACT.search(phrase) or STORE_LOCATION.search(phrase) or PUBLIC_HOTLINE.search(phrase):
            both = bool(re.search(r"\b(?:оба|обоих|всех)\s+магазин\w*\b", phrase))
            return self.stores(city, hotline=bool(PUBLIC_HOTLINE.search(phrase)),
                               fallback_city=both or bool(not city and STORE_LOCATION.search(phrase)))
        if re.fullmatch(r"(?:привет|здравствуйте|добрый (?:день|вечер)|что ты умеешь)[.!?]*", phrase):
            return HELP, []
        if re.search(r"сколько (?:всего )?(?:товаров|карточек|моделей|вариантов)(?: всего)? (?:на сайте|в каталоге|в магазине)", phrase):
            return f"Карточек в каталоге — {len(self.cat.products)}. Активных вариантов — {sum(bool(r['id']) for r in self.cat.rows)}.", []
        if self.options and phrase.rstrip(".!?") in {"еще","дальше"}:
            self.page = self.page+8 if self.page+8<len(self.options) else 0
            return self.menu()
        chosen = POSITION.search(phrase) or re.fullmatch(r"(номер\s+)([0-9]{1,5})[.!?]?", phrase)
        if chosen:
            n = int(chosen[1] if chosen[1] and chosen[1].isdigit() else chosen[2])
            if not 1<=n<=len(self.options):
                return "Укажите номер из последней подборки.", []
            exact, row = self.options[n-1]
            return self.show(row) if exact else self.choose([row["код"]])
        kit_details = bool(SIZES.search(phrase) or read_measurements(phrase) or CLOTHING_GROUP.search(phrase))
        kit_rest = SIZES.sub(" ", MEASUREMENTS.sub(" ", PART_OF_STORE.sub(" ", ADD_CART.sub(" ", phrase))))
        kit_rest = CLOTHING_GROUP.sub(" ", kit_rest)
        kit_followup = bool(city or cart_add or kit_details) and not (words(kit_rest) - FILLER)
        if self.last_kit and kit_followup:
            self.city = city or self.city
            return self.equipment(self.last_kit + " " + phrase)
        kit = BUILD_ORDER.search(phrase) or KIT.search(phrase) and (re.search(r"соб[еир]|подбер|подоб|нуж|куп|комплект|набор", phrase)
                                     or any(re.search(rule[0],phrase) for rule in SPORTS))
        if kit or self.pending_kit and (city or any(re.search(rule[0],phrase) for rule in SPORTS)):
            self.city = city or self.city
            return self.equipment((self.pending_kit + " " + phrase).strip())
        wants_card = bool(PHOTO_QUERY.search(phrase) or LINK_QUERY.search(phrase))
        query = PHOTO_QUERY.sub(" ", NEGATIVE_ITEM.split(phrase, 1)[0])
        if cart_add:
            query = re.sub(r"\bв корзин\w*\b|\b(?:добав\w*|полож\w*|закин\w*|кин\w*)\b", " ",query)
        if fit_request or self.fit_code and MEASUREMENTS.search(query):
            query = MEASUREMENTS.sub(" ",FIT_REQUEST.sub(" ",query))
            query = re.sub(r"\b(?:мне|мой|моя|подойд\w*|как|подобра\w*|выбрать|примерно|около|у меня)\b"," ",query)
        if price_reason:
            query = re.sub(r"\b(?:почему|за что|откуда|столько|такой|дорог\w*|дороже|высок\w*)\b"," ",query)
        query = self.shoe_size_query(query)
        if self.selected and any(re.search(ITEMS["Перчатки"], normalize(self.cat.products[code]["товар"]))
                                 for code in self.selected):
            query = OZ_SHORT.sub(r"\1 oz", query)
        anchors = attributes(query)
        if OZ_RANGE.search(query) or len(OZ_SIZE.findall(query)) > 1:
            return "Какой вес перчаток проверить первым? Укажите один вариант в унциях.", []
        quantity_match = QUANTITY.search(query) or LEADING_QUANTITY.search(query)
        quantity = None
        if quantity_match:
            label = quantity_match[1]
            quantity = int(label) if label.isdigit() else {"одну":1,"две":2,"два":2,"три":3,"четыре":4,"пять":5}[label]
        budget = budget_limit(query)
        term_phrase = BUDGET.sub(" ", query)
        if quantity_match:
            term_phrase = QUANTITY.sub(" ", LEADING_QUANTITY.sub(" ", term_phrase))
        term_phrase = PRICE_PER_PAIR.sub(" ", PART_OF_STORE.sub(" ", term_phrase))
        term_phrase = COLORS.sub(" ", EXACT_ATTRIBUTES.sub(" ", SIZES.sub(" ", term_phrase)))
        categories = [re.compile(pattern) for pattern in ITEMS.values() if re.search(pattern, query)]
        if LONG_SLEEVE.search(query):
            categories.append(LONG_SLEEVE)
        if re.search(ITEMS["Защита голени"], query) and SHIN_SLEEVE.search(query):
            categories.append(SHIN_SLEEVE)
        for pattern in categories:
            term_phrase = pattern.sub(" ", term_phrase)

        def same_category(row):
            return all(pattern.search(normalize(row["товар"])) for pattern in categories)

        direct = list(dict.fromkeys(code for token in re.findall(r"[\w-]{3,64}",term_phrase)
                                    for code in self.cat.articles.get(token,())
                                    if same_category(self.cat.products[code])))
        model_anchors = {m[0] for m in CONTAINS_ARTICLE.finditer(term_phrase) if any(c.isdigit() for c in m[0])}
        terms = words(term_phrase) - FILLER
        if terms & self.cat.vocabulary and categories:
            terms -= words("модели для начальных тренировок размеру материал подешевле")
        compare = bool(HISTORY.search(query))
        all_cities = all(name.casefold().rstrip("ь") in phrase for name in STORES) or bool(re.search(r"\b(?:оба|обоих|всех|другом)\s+(?:город\w*|магазин\w*|склад\w*)", phrase))
        previous = [code for code in self.selected if same_category(self.cat.products[code])]
        if not terms and (fit_request or self.fit_code and MEASUREMENTS.search(phrase)):
            if len(previous)==1:
                self.city = city or self.city
                return self.fit(previous[0],phrase)
            self.fit_pending = True
            self.fit_measures.update(read_measurements(phrase))
            return "Для какого товара подобрать размер? Откройте карточку или напишите модель.", [CATALOG_LINK]
        if not terms and previous and (anchors or city or all_cities or compare or quantity or budget is not None
                                       or wants_card or cart_add or price_reason or OFFER_QUERY.search(query)):
            self.filters.update(anchors)
            if city or all_cities:
                self.city = city
            if budget is not None:
                self.budget = budget
            self.compare = self.compare or compare
            selected_quantity = quantity or (self.last_quantity if cart_add else None)
            if self.page_variant is not None and not anchors and self.budget is None:
                reply,links = self.show(self.page_variant,selected_quantity)
            else:
                reply,links = self.choose(previous,selected_quantity)
            return self.finish_offer(previous,reply,links,cart_add,price_reason)
        if wants_card and not (terms or categories):
            return "Какой товар показать? Напишите название или выберите модель в каталоге.", [CATALOG_LINK]
        if not (terms or categories):
            return HELP, []
        codes = direct or [code for code,tokens in self.cat.product_words.items()
                           if terms <= tokens and same_category(self.cat.products[code])
                           and all(
                               anchor in CONTAINS_ARTICLE.findall(normalize(self.cat.products[code]["товар"]))
                               or anchor == normalize(self.cat.products[code]["артикул"])
                               for anchor in model_anchors)]
        if not codes:
            if not categories and not model_anchors and not terms & self.cat.vocabulary and not STOREFRONT_INTENT.search(query):
                return HELP, []
            self.selected = []
            self.options = []
            if categories:
                kind = next((name for name, regex in ITEMS.items() if re.search(regex,query)), "")
                return f"{MISSING} Категория — {kind.casefold()}.", [MANAGER_LINK]
            return MISSING, [MANAGER_LINK]
        if fit_request:
            self.selected = codes
            self.city = city
            if len(codes)==1:
                if self.fit_pending and self.fit_code is None:
                    self.fit_code = codes[0]
                return self.fit(codes[0],phrase)
            self.fit_pending = True
            return "Нашёл несколько моделей. Уточните одну, чтобы подобрать размер.", [link(self.cat.products[c]) for c in codes[:6]]
        self.filters = anchors
        self.fit_code = None
        self.last_kit = ""
        self.city = city
        self.budget = budget
        self.compare = compare
        reply,links = self.choose(codes, quantity)
        return self.finish_offer(codes,reply,links,cart_add,price_reason)

    def finish_offer(self, codes, reply, links, cart_add, price_reason):
        if price_reason:
            return self.explain_price(codes,reply,links)
        if cart_add and not self.cart and len(links)==1 and links[0].get("variant"):
            prompt = ("Укажите магазин для добавления в корзину." if not self.city else
                      "Для корзины проверьте другой магазин или уточните количество.")
            if self.display is not None:
                self.display = (self.display + "\n" if self.display else "") + prompt
            reply += "\n" + prompt
        return reply,links
