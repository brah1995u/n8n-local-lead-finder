import asyncio
import ipaddress
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .rules import YES, NOT_FOUND, UNKNOWN, ORDER_PROVIDERS, SOCIAL, host, is_host, marketplace, norm, official_candidate, phone

ORDER_WORDS = re.compile(r"pedir|pide|pedido|order|carrito|cistell|comand[ae]|/cart|checkout|compra|carta|menu|menú|domicili|contact|take.?away|tienda|shop|collection|producto", re.I)
ORDER_INTENT = re.compile(r'pedir|pide|pedido|order|ordenar|comand[ae]|comprar',re.I)
DELIVERY_WORDS = re.compile(r"a domicili(?:o)?|delivery|reparto|para llevar|recogida|take.?away|take.?out|per emportar|recollida", re.I)


def delivery_evidence(text):
    for match in DELIVERY_WORDS.finditer(text):
        context=norm(text[max(0,match.start()-60):match.end()+35])
        if re.search(r'\b(?:no (?:hacemos|ofrecemos|tenemos|realizamos|hay|fem|oferim)|sin servicio de|sense servei de)\b.{0,35}(?:domicili|delivery|reparto|llevar|recollida)',context):continue
        if re.search(r'(?:domicili\w*|delivery|take.?away).{0,15}(?:no disponible|not available|unavailable)',context):continue
        if re.search(r'\bno\s+(?:delivery|take.?away)\b',context):continue
        return True
    return False


def public_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return False
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)
    except (OSError, ValueError):
        return False


def parse_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    text = soup.get_text(" ", strip=True)
    links = [{"url": urljoin(url, a.get("href", "")), "text": a.get_text(" ", strip=True)} for a in soup.select("a[href]")]
    buttons = " ".join(e.get_text(" ", strip=True) + " " + str(e.get("aria-label", "")) + " " + str(e.get("value", "")) for e in soup.select("button, input[type=submit], [role=button]"))
    cart = re.search(r"anadir al carrito|agregar al carrito|add to cart|add to basket|anadir al pedido|\banadir\b|\bafegir\b", norm(buttons))
    checkout = re.search(r"finalizar (pedido|compra)|tramitar pedido|checkout|ver carrito|mi pedido|tu pedido|tu carrito|hacer pedido|realizar pedido|finalitzar (comanda|compra)|veure (cistell|carret)|(?:meva|teva|teua) comanda", norm(text + " " + buttons))
    provider = any(any(p in str(tag.get(attr, "")) for p in ORDER_PROVIDERS) for tag in BeautifulSoup(html, "html.parser").find_all(["script", "iframe", "a"]) for attr in ("href", "src"))
    product_form = bool(soup.select('form[action*="/cart/add"], form[action*="/cart/add.js"]'))
    cart_link = any(re.search(r"/cart(?:[/?#]|$)",l["url"]) for l in links)
    order = bool((cart and checkout) or (product_form and cart_link) or (provider and ORDER_INTENT.search(norm(text + " " + buttons))))
    channels = {marketplace(l["url"]) for l in links if marketplace(l["url"])}
    if any(any(is_host(host(l["url"]), s) for s in ("wa.me", "whatsapp.com")) for l in links):
        channels.add("WhatsApp")
    if any(l["url"].startswith("tel:") for l in links):
        channels.add("Телефон")
    if order:
        channels.add("Власна система")
    return {"text": text[:45000], "links": links, "ordering": order, "catalog":cart_link and any('/products/' in l['url'] for l in links), "delivery": delivery_evidence(text), "channels": sorted(channels), "substantial": len(text) >= 120}


async def fetch_page(url):
    if not public_url(url):
        return None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": "CortexLeadResearch/1.0 (public business information)"}) as client:
            current = url
            for _ in range(5):
                if not public_url(current):
                    return None
                r = await client.get(current)
                if r.is_redirect:
                    current = urljoin(current, r.headers.get("location", ""))
                    continue
                if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
                    return None
                parsed = parse_page(r.text[:1_500_000], current)
                if parsed["substantial"] and not re.search(r"just a moment|verify you are human|access denied|captcha", parsed["text"][:1000], re.I):
                    if parsed["ordering"] or not re.search(r"<script[^>]+src=|__NEXT_DATA__|data-reactroot|gloriafood|flipdish",r.text,re.I):
                        return {**parsed, "url": current}
                break
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            context = await browser.new_context(ignore_https_errors=False, service_workers="block")
            async def guard(route):
                if route.request.resource_type in ("image", "media", "font") or not public_url(route.request.url):
                    await route.abort()
                else:
                    await route.continue_()
            await context.route("**/*", guard)
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(1500)
            html, final_url = await page.content(), page.url
            await browser.close()
            parsed = parse_page(html, final_url)
            if response and response.status < 400 and parsed["substantial"] and not re.search(r"just a moment|verify you are human|access denied|captcha", parsed["text"][:1000], re.I):
                return {**parsed, "url": final_url}
    except Exception:
        return None
    return None


def identity_matches(raw, text):
    digits = re.sub(r"\D", "", text)
    tel = phone(raw.get("phone"))
    if tel and tel[-9:] in digits:
        return True
    stop = {"pizzeria", "kebab", "burger", "gandia", "playa", "platja", "restaurante", "bar", "pizza", "de", "la", "el", "y"}
    words = [w for w in re.findall(r"[a-z0-9]+", norm(raw.get("title") or raw.get("name"))) if w not in stop and len(w) > 2]
    return bool(words) and all(w in norm(text) for w in words[:3])


def branch_matches(raw, text):
    """Search evidence must identify this branch, rather than only its brand."""
    digits = re.sub(r"\D", "", text)
    tel = phone(raw.get("phone"))
    if tel and tel[-9:] in digits:
        return True
    location = norm(raw.get("city", ""))
    if not location and "gandia" in norm(raw.get("address", "")):
        location = "gandia"
    return identity_matches(raw,text) and bool(location and location in norm(text))


def merchant_listing(url):
    # Category/city/dish listings mention many restaurants and prove no individual channel.
    return bool(marketplace(url)) and bool(re.search(r"/(store|stores|restaurants?)[/-]",urlparse(url).path,re.I))


def owned_search_url(raw,url):
    if not official_candidate(url):return False
    stop={'pizzeria','pizza','kebab','burger','restaurante','bar','gandia','platja','playa','comida','para','llevar','the','co','del','de','por','horno','lena','cafeteria'}
    words=[w for w in re.findall(r'[a-z0-9]+',norm(raw.get('title') or raw.get('name'))) if len(w)>2 and w not in stop]
    domain=re.sub(r'[^a-z0-9]','',host(url))
    # Search directories and news articles can repeat the entire identity. A matching
    # business name in the domain (or an explicit branded hosting page) is also needed.
    if any(w in domain for w in words):return True
    hosted=('promotty.com','ueniweb.com','wixsite.com','wordpress.com','google.com/view')
    return any(s in host(url) for s in hosted) and any(w in norm(urlparse(url).path) for w in words)


async def inspect(raw, serp_results=None, search_complete=False):
    sources = list(dict.fromkeys(raw.get("_sources", []) + [u for u in (raw.get("url"), raw.get("website")) if u]))
    result = {"website_status": UNKNOWN, "ordering_status": UNKNOWN, "website": "", "delivery": False, "channels": [], "sources": sources, "evidence": [], "checked_at": datetime.now(timezone.utc).isoformat()}
    extras = raw.get("additionalInfo", {})
    for section in extras.values():
        for entry in section if isinstance(section,list) else []:
            if isinstance(entry, dict) and any(v is True and DELIVERY_WORDS.search(k) for k, v in entry.items()):
                result["delivery"] = True
    if delivery_evidence(" ".join(str(raw.get(k,"")) for k in ("title","description","categoryName","categories"))):
        result["delivery"] = True
    candidates = []
    website = raw.get("website") or ""
    if website:
        if marketplace(website):
            result["channels"].append(marketplace(website))
            result["delivery"] = True
        elif any(is_host(host(website), s) for s in SOCIAL):
            result["evidence"].append("У картці вказано соціальну сторінку замість сайту")
        elif official_candidate(website):
            candidates.append(website)
    for item in serp_results or []:
        url = item.get("url", "")
        snippet = item.get("title", "") + " " + item.get("description", "")
        if not branch_matches(raw, snippet):
            continue
        if delivery_evidence(snippet) and (not marketplace(url) or merchant_listing(url)):
            result["delivery"] = True
            result["sources"].append(url)
            result["evidence"].append("Доставка/самовивіз згадані у зіставленому публічному результаті пошуку")
        if merchant_listing(url):
            result["channels"].append(marketplace(url))
            result["delivery"] = True
            result["sources"].append(url)
        elif owned_search_url(raw,url):
            candidates.append(url)
    uncertain = False
    for url in list(dict.fromkeys(candidates))[:3]:
        page = await fetch_page(url)
        trusted_maps_host = bool(website and host(url)==host(website))
        if not page or not identity_matches(raw, page["text"]) or (not trusted_maps_host and not branch_matches(raw,page["text"])):
            uncertain = True
            continue
        result["website_status"], result["website"] = YES, page["url"]
        result["sources"].append(page["url"])
        pages = [page]
        # Inspect ordering/menu links including branded external ordering providers.
        relevant = [l for l in page["links"] if ORDER_WORDS.search(l["text"] + " " + l["url"]) and (is_host(host(l["url"]),host(page["url"])) or is_host(host(page["url"]),host(l["url"])) or any(p in l["url"] for p in ORDER_PROVIDERS) or (official_candidate(l['url']) and ORDER_INTENT.search(l['text'])))]
        relevant.sort(key=lambda l: 0 if ORDER_INTENT.search(l["text"]+" "+l["url"]) else 1)
        failed = False
        for target in list(dict.fromkeys(l["url"] for l in relevant))[:5]:
            if target.split("#")[0].rstrip("/") == page["url"].split("#")[0].rstrip("/"):
                continue
            child = await fetch_page(target)
            if child:
                pages.append(child)
                result["sources"].append(child["url"])
            else:
                failed = True
        if not any(p['ordering'] for p in pages):
            product = next((l['url'] for p in pages if p.get('catalog') for l in p['links'] if '/products/' in l['url']),None)
            if product:
                child=await fetch_page(product)
                if child:
                    pages.append(child)
                    result['sources'].append(child['url'])
                else:failed=True
        result["delivery"] |= any(p["delivery"] for p in pages)
        result["channels"] += [c for p in pages for c in p["channels"]]
        unresolved_order_link=bool(re.search(r'pedidos?\.|comand[ae]s?\.|/pedido(?:[/?#]|$)|/comand[ae]s?(?:[/?#]|$)|/ordering(?:[/?#]|$)|/order(?:[/?#]|$)',page['url'],re.I)) or any(ORDER_INTENT.search(l['text']) and official_candidate(l['url']) and urlparse(l['url']).scheme in ('http','https') for l in relevant)
        result["ordering_status"] = YES if any(p["ordering"] for p in pages) else UNKNOWN if failed or unresolved_order_link else NOT_FOUND
        result["evidence"].append("Сайт зіставлено з назвою або телефоном; перевірено головну та посилання на меню/замовлення")
        break
    if result["website_status"] == UNKNOWN and not candidates and search_complete:
        result["website_status"] = NOT_FOUND
        result["ordering_status"] = NOT_FOUND
        result["evidence"].append("Офіційний сайт і власне замовлення не виявлені у двох завершених цільових пошуках")
    if uncertain and result["website_status"] != YES:
        result["evidence"].append("Сайт-кандидат недоступний або належність закладу не підтверджена")
    result["channels"] = sorted(set(result["channels"]))
    if result["delivery"]:
        result["channels"].append("Доставка")
    if phone(raw.get("phone")):
        result["channels"].append("Телефон")
    result["channels"] = sorted(set(result["channels"]))
    result["sources"] = list(dict.fromkeys(result["sources"]))[:12]
    return result
