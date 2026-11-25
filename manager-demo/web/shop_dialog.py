"""Storefront answers from a public feed and fixed handlers."""
import re
import unicodedata

from archive_catalog import COLORS, SIZES, words
from manager_catalog import OZ_SHORT, POSITION
from manager_knowledge import OZ_RANGE, OZ_SIZE, expand_terms, resolve_context
from shop_knowledge import data, expand_shop_terms, policy_answer, scope_answer
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
    r"@|(?:\+?\d[\d .()\-]{8,}\d)|\b(?:снилс|паспорт\w*|cvv|cvc|мой телефон|мой адрес|меня зовут|"
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
                "номер пожалуйста плиз которые именно того этого варианта их к выйдет обе списком")
CONTAINS_ARTICLE = re.compile(r"(?<![\w-])[a-zа-я0-9]+(?:-[a-zа-я0-9]+)+(?![\w-])", re.I)
BUDGET = re.compile(r"\b(?:не дороже|до|бюджет(?:ом)?)\s*(\d+(?:[ \u00a0]\d{3})*)\s*"
                    r"(тыс\w*|к\b)?\s*(?:₽|руб\w*)?(?!\d|\s*(?:oz|унц|см|кг)\b)")
QUANTITY = re.compile(r"\b(\d{1,3}|одну|две|два|три|четыре|пять)\s+(?:штук\w*|шт\.?|пар\w*)\b")
STOREFRONT_INTENT = re.compile(r"\b(?:ищу|найди\w*|покажи\w*|подбер\w*|прода[её]\w*|"
                               r"магазин\w*|ассортимент\w*|достав\w*|в наличии|цена|стоит|стоимость|сколько стоят)\b")
STORE_LOCATION = re.compile(r"\b(?:офлайн[- ]магазин\w*|где вход|открыт\w*|выходно\w*|воскресень\w*)\b")
PUBLIC_HOTLINE = re.compile(r"\b(?:бесплатн\w* номер\w*|общ\w* номер\w*)\b")
NEGATIVE_ITEM = re.compile(r"\s+(?:не\s+(?!дороже\b|более\b|дешевле\b|менее\b)|без\s+)")
PART_OF_STORE = re.compile(r"\b(?:севастопол\w*|симферопол\w*)\b")
EXACT_ATTRIBUTES = re.compile(r"\b(?:размер\w*|рост(?:ом|а)?)\s+(\d+(?:[.,]\d+)?)\b")
SAMBO_SHOES = re.compile(r"\bсамбовк\w*[^?]*?\bтолько\s+(?:обувь|ботинки)\b")
LONG_SLEEVE = re.compile(r"\bдлинн\w*\s+рукав\w*\b")
SHIN_SLEEVE = re.compile(r"\bчул(?:ок|к\w*)\b")
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


def link(row, exact=False):
    label = row["товар"] + (f" · {row['вариант']}" if exact else "")
    href = "/shop/product?id=" + row["код"][1:]
    if exact and row["id"]:
        href += "&sku=" + row["id"]
    return {"label":label,"href":href}


def city_in(phrase):
    selected = [city for city in STORES if city.casefold().rstrip("ь") in phrase]
    return selected[0] if len(selected)==1 else None


def budget_limit(phrase):
    match = BUDGET.search(phrase)
    return int(re.sub(r"\s", "", match[1])) * (100_000 if match[2] else 100) if match else None


def attributes(text):
    sizes = [m[0] for m in SIZES.finditer(OZ_SIZE.sub(" ", text))]
    sizes.extend(EXACT_ATTRIBUTES.findall(text))
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

    def equipment(self, phrase):
        explicit = tuple(name for name in ("Перчатки","Бинты","Капа","Шлем","Защита голени","Шорты",
                                           "Рашгард","Кимоно","Пояс","Футболка","Легинсы")
                         if re.search(ITEMS[name], phrase))
        sport = next((sport for sport in SPORTS if re.search(sport[0], phrase)), None)
        slots = explicit if len(explicit)>1 else (sport[2] if sport else ())
        if "одежд" in phrase and len(explicit)<2:
            slots = tuple(slot for slot in slots if slot in {"Рашгард","Шорты","Кимоно","Футболка","Легинсы"})
        if not slots:
            self.pending_kit = phrase
            return "Для какого спорта собрать комплект? Можно указать вещи и общий бюджет.", []
        self.pending_kit = ""
        self.options = []
        apparel = {"Рашгард","Шорты","Кимоно","Футболка","Легинсы"}
        size_labels = [m[0] for m in SIZES.finditer(phrase) if not re.search(r"oz|унц|кг",m[0])]
        size_labels += re.findall(r"\bразмер\w*\s+(\d+)(?!\d|\s*(?:oz|унц|кг))",phrase)
        sizes = words(" ".join(size_labels),variant=True)
        ranked, missing, used = [], [], set()
        for slot in slots:
            best = {}
            exact = bool(sizes and slot in apparel)
            for row in self.cat.rows:
                stock = row["склады"]
                title = normalize(row["товар"])
                if (row["код"] in used or not row["id"] or not row["price_cents"]
                        or not re.search(ITEMS[slot], title)
                        or (exact and not sizes<=words(row["вариант"],variant=True))
                        or not (stock[self.city] if self.city else any(stock.values()))):
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
            choice = min(best.values(),key=lambda row:(row["price_cents"],row["товар"],row["id"]))
            used.add(choice["код"])
            ranked.append((slot,choice,exact))
        if not ranked:
            return "Сейчас не нашёл доступных позиций для этого комплекта. Уточните состав или магазин.", []
        place = f" в магазине «{self.city}»" if self.city else " по двум магазинам"
        lines = [f"Подборка{place}. Названия ведут к карточкам, где можно выбрать размер."]
        if not self.city:
            lines.append("Позиции могут быть в разных магазинах.")
        for slot,row,exact in ranked:
            variant = f' ({row["вариант"]})' if exact else ""
            price = rub(row["price_cents"])
            lines.append(f'{slot} · {row["товар"]}{variant} · {price if exact else "от "+price}')
        total = sum(row["price_cents"] for _,row,_ in ranked)
        lines.append(f"Сумма найденных позиций от {rub(total)}. Цена зависит от варианта.")
        budget = budget_limit(phrase)
        if budget is not None:
            if total>budget:
                lines.append("Эта подборка выше указанного бюджета.")
            elif missing:
                lines.append(f"В бюджете остаётся {rub(budget-total)} на недостающие позиции.")
        if missing:
            lines.append("Не нашёл: " + ", ".join(missing) + ".")
            lines.append("Полный комплект сейчас недоступен. Наличие недостающих позиций можно уточнить у магазина.")
        return "\n".join(lines), [link(row,exact) for _,row,exact in ranked]

    def active_variant(self, idx):
        row = self.cat.rows[idx]
        return (self.attrs <= self.cat.variant_words[idx]
                and (self.budget is None or 0 < row["price_cents"] <= self.budget))

    def menu(self):
        part = self.options[self.page:self.page+8]
        title = f"Нашёл варианты. {self.city}. Выберите номер строки для подробностей." if self.city else (
            "Нашёл варианты. Выберите номер строки или уточните размер и цвет."
        )
        lines, links = [title], []
        for number, (exact, row) in enumerate(part,self.page+1):
            label = row["товар"] + (" · " + row["вариант"] if exact and row["id"] else "")
            links.append({**link(row,exact),"label":f"№{number} {label}"})
            variants = ([row] if exact else
                        [self.cat.rows[i] for i in self.cat.by_product[row["код"]]
                         if self.active_variant(i)])
            active = [variant for variant in variants if variant["id"]]
            if not active:
                lines.append(f"№{number} {label} · нет активных вариантов")
                continue
            prices = sorted({variant["price_cents"] for variant in active if variant["price_cents"]})
            if len(prices) > 1:
                price = f"{rub(prices[0])} – {rub(prices[-1])}"
            elif prices:
                price = rub(prices[0])
            else:
                price = "цена не указана"
            stock = "; ".join(f"{city} — {sum(variant['склады'][city] for variant in active)} шт."
                              for city in ([self.city] if self.city else STORES))
            lines.append(f"№{number} {label} · {price}. {stock.rstrip('.')}.")
        if len(self.options)>8:
            lines.append(f"Показано {self.page+1}–{self.page+len(part)} из {len(self.options)}. Напишите «ещё» для продолжения.")
        return "\n".join(lines), links

    def show(self, row, quantity=None):
        self.options = []
        self.selected = [row["код"]]
        self.filters = attributes(normalize(row["вариант"])) if row["id"] else {}
        if not row["id"]:
            return f'{row["товар"]}. У карточки пока нет активных вариантов для покупки.', [link(row)]
        lines = [f'{row["товар"]} · {row["вариант"]}']
        if row["price_cents"]:
            lines.append("Цена сейчас " + rub(row["price_cents"]) + ".")
            if row["old_price_cents"] > row["price_cents"]:
                lines.append("На карточке зачёркнута цена " + rub(row["old_price_cents"]) + ".")
        for city in [self.city] if self.city else STORES:
            lines.append(f'{city} — доступно {row["склады"][city]} шт.')
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
        return "\n".join(lines), [link(row, True)]

    def choose(self, codes, quantity=None):
        indices = [i for code in codes for i in self.cat.by_product[code]]
        rows = [self.cat.rows[i] for i in indices if self.active_variant(i)]
        if not rows:
            self.options = []
            self.selected = codes
            if self.budget is not None:
                return f"Для выбранных товаров нет опубликованных вариантов до {rub(self.budget)}.", []
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
                return text, [link(product)]
            return "Для выбранных моделей такой размер или цвет не найден.", []
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
            restricted = scope_answer(normalize(question)) or blocked(question)
            if restricted:
                result = restricted
                continue
            question = SAMBO_SHOES.sub("ботинки самбо", question.casefold())
            question = expand_shop_terms(question)
            _, clarification = resolve_context(question)
            result = (clarification, []) if clarification else self.turn(expand_terms(question).casefold())
        return result

    def turn(self, phrase):
        city = city_in(phrase)
        if policy := policy_answer(phrase):
            if city and re.search(r"\b(?:самовывоз\w*|забрать\w*)\b", phrase) and not re.search(r"\bналичн\w*\b", phrase):
                contacts, links = self.stores(city)
                return policy[0] + "\n" + contacts, links
            return policy
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
        kit = KIT.search(phrase) and (re.search(r"соб[еир]|подбер|подоб|нуж|куп|комплект|набор", phrase)
                                     or any(re.search(rule[0],phrase) for rule in SPORTS))
        if kit or self.pending_kit and (city or any(re.search(rule[0],phrase) for rule in SPORTS)):
            self.city = city or self.city
            return self.equipment((self.pending_kit + " " + phrase).strip())
        query = NEGATIVE_ITEM.split(phrase, 1)[0]
        if self.selected and any(re.search(ITEMS["Перчатки"], normalize(self.cat.products[code]["товар"]))
                                 for code in self.selected):
            query = OZ_SHORT.sub(r"\1 oz", query)
        anchors = attributes(query)
        if OZ_RANGE.search(query) or len(OZ_SIZE.findall(query)) > 1:
            return "Какой вес перчаток проверить первым? Укажите один вариант в унциях.", []
        quantity_match = QUANTITY.search(query)
        quantity = None
        if quantity_match:
            label = quantity_match[1]
            quantity = int(label) if label.isdigit() else {"одну":1,"две":2,"два":2,"три":3,"четыре":4,"пять":5}[label]
        budget = budget_limit(query)
        term_phrase = PART_OF_STORE.sub(" ", EXACT_ATTRIBUTES.sub(" ", QUANTITY.sub(" ", BUDGET.sub(" ", query))))
        term_phrase = COLORS.sub(" ", SIZES.sub(" ", term_phrase))
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
        if not terms and previous and (anchors or city or all_cities or compare or quantity or budget):
            self.filters.update(anchors)
            if city or all_cities:
                self.city = city
            if budget is not None:
                self.budget = budget
            self.compare = self.compare or compare
            return self.choose(previous, quantity)
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
                return f"{MISSING} Категория — {kind.casefold()}.", []
            return MISSING, []
        self.filters = anchors
        self.city = city
        self.budget = budget
        self.compare = compare
        return self.choose(codes, quantity)
