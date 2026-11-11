import asyncio
import pytest
from app.rules import *
from app.inspector import parse_page, inspect, public_url, branch_matches, merchant_listing, owned_search_url, delivery_evidence


def raw(**values):
    return {"title":"Pizza Luna","categoryName":"Pizzería","phone":"622123456","address":"Carrer Major 1, Gandia",**values}


@pytest.mark.parametrize("channel",["Glovo","Just Eat","WhatsApp"])
def test_marketplace_and_manual_whatsapp_do_not_exclude(channel):
    q=qualify(raw(),{"website_status":NOT_FOUND,"ordering_status":NOT_FOUND,"delivery":True,"channels":[channel]})
    assert q["eligible"] and q["priority"]=="A" and q["status"]=="Новий"


def test_own_ordering_excludes_and_directory_is_not_owned_site():
    assert not qualify(raw(),{"website_status":YES,"ordering_status":YES,"delivery":True})["eligible"]
    assert not official_candidate("https://www.just-eat.es/restaurants-luna/menu")
    assert official_candidate("https://pizza-luna.ueniweb.com")


@pytest.mark.parametrize("value",[UNKNOWN,NOT_FOUND])
def test_uncertainty_is_not_ready_if_site_exists(value):
    q=qualify(raw(),{"website_status":YES,"ordering_status":value,"delivery":True})
    assert q["status"]==("Перевірити" if value==UNKNOWN else "Новий")


def test_seasonal_coastal_and_suppression():
    assert qualify(raw(temporarilyClosed=True),{"website_status":NOT_FOUND,"delivery":True})["status"]=="Перевірити"
    assert qualify(raw(_coastal=True),{"website_status":NOT_FOUND})["status"]=="Перевірити"
    assert not qualify(raw(),{"delivery":True},True)["eligible"]
    assert not qualify(raw(title="Burger King Gandia"),{"delivery":True})["eligible"]
    assert not qualify(raw(title="Restaurante La Tagliatella | Gandía"),{"delivery":True})["eligible"]
    assert not qualify(raw(title="VIPS Smart"),{"delivery":True})["eligible"]


def test_branches_and_phone_normalization():
    assert phone("+34 622 12 34 56")==phone("622123456")
    assert dedupe(raw(address="Street 1"))!=dedupe(raw(address="Street 2"))
    assert dedupe(raw(placeId="ChIJone"))=="google:ChIJone"


def test_menu_and_reservations_do_not_count_as_ordering():
    result=parse_page('<h1>Pizza Luna</h1><a href="tel:622123456">Reservar mesa</a><a href="https://wa.me/34622123456">Pedido por WhatsApp</a>',"https://luna.example")
    assert not result["ordering"] and "WhatsApp" in result["channels"]
    assert parse_page('<button>Añadir al carrito</button><button>Finalizar pedido</button>',"https://luna.example")["ordering"]
    assert parse_page('<a href="https://www.foodbooking.com/ordering/restaurant/menu">Pedir online</a>',"https://luna.example")["ordering"]


def test_unavailable_site_stays_unknown(monkeypatch):
    async def unavailable(url):return None
    monkeypatch.setattr("app.inspector.fetch_page",unavailable)
    i=asyncio.run(inspect(raw(website="https://luna.example"),[],True))
    assert i["website_status"]==UNKNOWN and i["ordering_status"]==UNKNOWN


def test_no_site_requires_completed_search():
    i=asyncio.run(inspect(raw(),[],False))
    assert i["website_status"]==UNKNOWN
    assert asyncio.run(inspect(raw(),[],True))["website_status"]==NOT_FOUND


@pytest.mark.parametrize("url",["http://127.0.0.1/","http://169.254.169.254/","http://[::1]/","file:///etc/passwd","https://localhost/","http://example.com:bad/"])
def test_private_targets_blocked(url):
    assert not public_url(url)


def test_other_branch_and_category_listing_do_not_prove_delivery():
    r=raw(title="The Fitzgerald Gandía",city="Gandia")
    assert not branch_matches(r,"The Fitzgerald a domicilio en Albal")
    assert branch_matches(r,"The Fitzgerald Gandía para llevar")
    assert not merchant_listing("https://www.ubereats.com/es/dish/gandia-es-vc/waffle")
    assert merchant_listing("https://glovoapp.com/es/es/gandia/stores/pizza-luna-gandia")


@pytest.mark.parametrize("url",["https://x.com/PEKOGANDIA","https://pueblito.es/gandia/pizza-luna","https://pizzeriaespana.com/c/gandia-46/"])
def test_social_and_catalog_are_not_official_sites(url):
    assert not official_candidate(url)


def test_trusted_maps_site_follows_order_link_before_contact(monkeypatch):
    seen=[]
    async def fetch(url):
        seen.append(url)
        html='<h1>Pizza Luna Gandia</h1>'+'pizza '*30
        if 'shops.' in url:html+='<button>Añadir al carrito</button><button>Finalizar pedido</button>'
        else:html+='<a href="/contacto">Contacto</a><a href="https://shops.luna.example/">PIDE YA</a>'
        return {**parse_page(html,url),'url':url}
    monkeypatch.setattr('app.inspector.fetch_page',fetch)
    result=asyncio.run(inspect(raw(website='https://luna.example/'),[],True))
    assert result['ordering_status']==YES
    assert seen[1]=='https://shops.luna.example/'


def test_custom_cart_and_shopify_form_are_detected():
    assert parse_page('<button>Añadir</button><h2>Tu pedido</h2><p>Tu carrito está vacío</p>','https://luna.example/pedido')['ordering']
    assert parse_page('<form action="/cart/add"><button name="add">Agregar</button></form><a href="/cart">0</a>','https://luna.example/products/pizza')['ordering']


def test_directory_article_does_not_become_official_site():
    r=raw(title="Jocho's")
    assert not owned_search_url(r,'https://lamargot.es/jochos-en-gandia/')
    assert owned_search_url(raw(),'https://pizzaluna.es/carta')


def test_order_link_without_verifiable_cart_stays_unknown(monkeypatch):
    async def fetch(url):
        html='<h1>Pizza Luna Gandia</h1>'+'pizza '*30
        if 'luna.example' in url:html+='<a href="https://pedidos.vendor.example/luna">Iniciar pedido</a>'
        return {**parse_page(html,url),'url':url}
    monkeypatch.setattr('app.inspector.fetch_page',fetch)
    result=asyncio.run(inspect(raw(website='https://luna.example'),[],True))
    assert result['ordering_status']==UNKNOWN


def test_valencian_cart_and_explicit_delivery_negation():
    assert parse_page('<button>Afegir al cistell</button><button>Finalitzar comanda</button>','https://luna.example')['ordering']
    assert parse_page('<a href="https://foodbooking.com/ordering/luna">Fes la teua comanda</a>','https://luna.example')['ordering']
    assert delivery_evidence('Servei a domicili i recollida en el local')
    assert not delivery_evidence('No hacemos envíos a domicilio')
    assert not delivery_evidence('A domicilio no disponible')
