import hashlib
import re
import unicodedata
from urllib.parse import urlparse

YES, NOT_FOUND, UNKNOWN = "present", "not_found", "unknown"
STATES = {YES: "Є", NOT_FOUND: "Не виявлено після перевірки", UNKNOWN: "Не вдалося перевірити"}
MARKETPLACES = {"glovoapp.com": "Glovo", "glovoapp.es": "Glovo", "just-eat.es": "Just Eat", "ubereats.com": "Uber Eats"}
SOCIAL = ("facebook.com", "instagram.com", "tiktok.com", "linktr.ee", "wa.me", "whatsapp.com", "x.com", "twitter.com", "youtube.com")
DIRECTORIES = ("tripadvisor.", "restaurantguru.", "google.", "reserva-restaurante-menu.", "yelp.", "paginasamarillas.", "pueblito.es", "pizzeriaespana.com", "gastrorank.", "opentable.", "mapquest.", "wanderlog.", "wanderboat.", "espainfo.", "saforguia.", "denia.com", "gandiazoom.", "toppingsandsalads.", "iberinform.", "eleconomista.", "bandcamp.")
CHAINS = ("mcdonald", "burger king", "domino", "telepizza", "pizza hut", "kfc", "subway", "taco bell", "popeyes", "five guys", "goiko", "la tagliatella", "vips smart")
SUPPRESSED = {"not interested", "не контактувати", "не підходить", "відмова", "stop", "клієнти", "клієнт", "оплачено", "customer", "won"}
ORDER_PROVIDERS = ("gloriafood.com", "foodbooking.com", "flipdish.com", "flipdish.ie", "order.store", "last.app", "covermanager.com/delivery", "pidedirecto.", "qamarero.com/order")


def norm(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(value or "").lower()) if not unicodedata.combining(c)).split())


def phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 9:
        digits = "34" + digits
    return "+" + digits if 9 <= len(digits) <= 15 else ""


def host(url):
    return (urlparse(url if "://" in str(url) else "https://" + str(url)).hostname or "").lower().removeprefix("www.")


def is_host(domain, suffix):
    return domain == suffix or domain.endswith("." + suffix)


def marketplace(url):
    return next((label for domain, label in MARKETPLACES.items() if is_host(host(url), domain)), None)


def official_candidate(url):
    domain = host(url)
    return bool(domain and not marketplace(url) and not any(is_host(domain, s) for s in SOCIAL) and not any(d in domain for d in DIRECTORIES))


def niche(raw):
    text = norm(" ".join(str(raw.get(k, "")) for k in ("title", "name", "categoryName", "categories", "description")))
    if any(s in text for s in ("kebab", "doner", "durum", "shawarma")):
        return "Кебаб"
    if any(s in text for s in ("pizza", "pizzeria", "pizzería")):
        return "Піцерія"
    if any(s in text for s in ("burger", "hamburgues", "smash")):
        return "Бургерна"
    if any(s in text for s in ("fast food", "comida rapida", "menjar rapid", "comida para llevar", "takeaway", "tacos", "pollo frito")):
        return "Фастфуд"
    return None


def dedupe(raw):
    pid = raw.get("placeId") or raw.get("place_id")
    if pid:
        return "google:" + pid
    identity = "|".join([norm(raw.get("title") or raw.get("name")), phone(raw.get("phone")), norm(raw.get("address"))])
    return "fallback:" + hashlib.sha256(identity.encode()).hexdigest()


def suppression_keys(raw):
    result = [dedupe(raw)]
    if raw.get("placeId"):
        result.append("google:" + raw["placeId"])
    tel = phone(raw.get("phone"))
    if tel:
        result.append("tel:" + tel)
    return list(set(result))


def qualify(raw, inspection, suppressed=False):
    category = niche(raw)
    title = norm(raw.get("title") or raw.get("name"))
    if suppressed:
        return {"eligible": False, "reason": "Попередня відмова або чинний клієнт", "priority": "C", "status": "Не контактувати"}
    if not category or any(chain in title for chain in CHAINS):
        return {"eligible": False, "reason": "Ніша або велика мережа поза фокусом", "priority": "C", "status": "Не підходить"}
    if raw.get("permanentlyClosed"):
        return {"eligible": False, "reason": "Позначений як закритий назавжди", "priority": "C", "status": "Не підходить"}
    website, ordering = inspection.get("website_status", UNKNOWN), inspection.get("ordering_status", UNKNOWN)
    if website == YES and ordering == YES:
        return {"eligible": False, "reason": "Виявлені сайт і власні онлайн-замовлення", "priority": "C", "status": "Не підходить"}
    delivery = inspection.get("delivery", False)
    coastal = bool(raw.get("_coastal"))
    if not delivery and not coastal:
        return {"eligible": False, "reason": "Доставка/самовивіз і пляжний потенціал не підтверджені", "priority": "C", "status": "Перевірити"}
    ready = bool(phone(raw.get("phone"))) and delivery and not raw.get("temporarilyClosed") and not raw.get('_review_reason') and (website == NOT_FOUND or (website == YES and ordering == NOT_FOUND))
    priority = "A" if ready and website == NOT_FOUND else "B" if ready else "C"
    reason = "Після перевірки офіційний сайт не виявлено. Пропозиція: власні замовлення й автоматизація приймання." if website == NOT_FOUND else "На сайті власні онлайн-замовлення не виявлено. Пропозиція: кошик, самовивіз і доставка." if ordering == NOT_FOUND else "Потрібно перевірити сайт або спосіб оформлення замовлення перед дзвінком."
    if coastal and not delivery:
        reason = "Пляжний район: перевірити попит і спосіб приймання замовлень. Перевантаження — гіпотеза."
    if raw.get("temporarilyClosed"):
        reason += " Є позначка тимчасового закриття; перевірити сезонність."
    if raw.get('_review_reason'):
        reason += ' '+raw['_review_reason']
    return {"eligible": True, "reason": reason, "priority": priority, "status": "Новий" if ready else "Перевірити", "niche": category}
