#!/usr/bin/env python3
"""Vigila varias tiendas (One Piece TCG) y avisa por Telegram de:
  - productos NUEVOS (aunque salgan agotados / en reserva)
  - REPOSICIONES (un producto que estaba agotado y vuelve a estar disponible)

Uso:
  python watch_tiendas.py                 -> ejecución normal (la programas cada 15 min)
  python watch_tiendas.py --probar        -> prueba qué tiendas responden (no guarda ni avisa)
  python watch_tiendas.py --diag tienda1.com tienda2.es   -> muestra qué lee el script en esas tiendas

Dependencias: pip install requests beautifulsoup4
"""
import html
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = Path(__file__).with_name("tiendas_estado.json")
TIMEOUT = 20
MAX_FALLOS = 8      # ejecuciones seguidas fallando antes de avisarte de que una tienda no responde
MAX_MENSAJES = 20   # máximo de mensajes completos por ejecución (el resto va resumido)
HILOS = 8
DEBUG = False

# Plataforma de cada tienda (deducida de las URLs del chat). "auto" = probar todas las estrategias.
STORES = {
    "shopify": [
        "universetcg.com", "cardlantis.de", "tcg.otakuya.es", "spaceshiptcg.com",
        "kamehousecards.com", "factorycardstcg.com", "darkritualtcg.es", "allintcg.com",
        "totalcards.net", "sarumangames.es",
    ],
    "woocommerce": [
        "kabenzots.com", "flashstore.es", "arte9.com", "pocketpiecetcg.com", "zialtcg.com",
        "empiregames.es", "ingeniobcn.com", "cartinhabrilhante.com", "manavortex.es",
        "montalfan.com", "somniacrevillent.com", "geeklogia.shop", "comicstores.es",
        "hobbyshopwargames.es", "tcgfusion.com", "homelands.es", "vicoltcg.com", "kurogami.com",
    ],
    "prestashop": [
        "lacuevaroja.com", "dungeonmarvels.com", "masterofgames.es", "dashu.es",
        "cartooncorp.net", "minasdemoria.com", "padis-store.com", "latiendascum.com",
        "quinto-elemento.es", "garhis.es",
    ],
    "auto": [
        "miceliongames.com", "frikinexo.com", "collectorage.com", "topdeck.es",
        "frikibunker.es", "only-cards.com", "herofreaks.com",
    ],
}

# Nombre que se muestra en los avisos (si no está aquí, se deduce del dominio)
NOMBRES = {
    "universetcg.com": "Universe TCG", "cardlantis.de": "Cardlantis", "tcg.otakuya.es": "Otakuya",
    "spaceshiptcg.com": "Spaceship TCG", "kamehousecards.com": "Kame House",
    "factorycardstcg.com": "Factory Cards", "darkritualtcg.es": "Dark Ritual TCG",
    "allintcg.com": "All In TCG", "totalcards.net": "Total Cards", "sarumangames.es": "Saruman Games",
    "kabenzots.com": "Kabenzots", "flashstore.es": "Flashstore", "arte9.com": "Arte9",
    "pocketpiecetcg.com": "Pocket Piece TCG", "zialtcg.com": "ZIAL TCG", "empiregames.es": "Empire Games",
    "ingeniobcn.com": "Ingenio BCN", "cartinhabrilhante.com": "Cartinha Brilhante",
    "manavortex.es": "Mana Vortex", "montalfan.com": "Montalfan", "somniacrevillent.com": "Somnia Crevillent",
    "geeklogia.shop": "Geeklogia", "comicstores.es": "Comic Stores", "hobbyshopwargames.es": "Hobby Shop Wargames",
    "tcgfusion.com": "TCG Fusion", "homelands.es": "Homelands", "vicoltcg.com": "Vicol TCG",
    "kurogami.com": "Kurogami", "lacuevaroja.com": "La Cueva Roja", "dungeonmarvels.com": "Dungeon Marvels",
    "masterofgames.es": "Master of Games", "dashu.es": "Dashu", "cartooncorp.net": "Cartoon Corp",
    "minasdemoria.com": "Minas de Moria", "padis-store.com": "Padis Store", "latiendascum.com": "La Tienda Scum",
    "quinto-elemento.es": "Quinto Elemento", "garhis.es": "Garhis", "miceliongames.com": "Micelion",
    "frikinexo.com": "Frikinexo", "collectorage.com": "Collectorage", "topdeck.es": "Topdeck",
    "frikibunker.es": "Frikibunker", "only-cards.com": "Only Cards", "herofreaks.com": "Hero Freaks",
}

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
})

# Estricto (catálogos completos): tiene que decir "One Piece".
ONE_RE = re.compile(r"one[\s\-_]?piece", re.I)
# Laxo (resultados de búsqueda de "one piece"): también vale por códigos de set (OP-20, EB-06, ST-44...).
OP_RE = re.compile(r"one[\s\-_]?piece|\b(?:op|eb|st|prb|sd|dp|ib|gc|peb)[\s\-]?\d{1,2}\b", re.I)
AGOTADO_RE = re.compile(
    r"agotad|sin\s+stock|fuera\s+de\s+stock|sin\s+existencias|no\s+hay\s+stock|"
    r"out[\s\-]?of[\s\-]?stock|outofstock|no disponible|esgotad|sold[\s\-]?out",
    re.I,
)
PREVENTA_RE = re.compile(r"pre-?venta|pre-?order|precompra|pre-?compra|reserva", re.I)
# precios tipo 2.500,00 € / 199,79€ / 24.99 € / € 24,99
PRECIO_RE = re.compile(
    r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}|\d+\.\d{2})\s*€|€\s*(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}|\d+\.\d{2})"
)
# Pon True para no recibir avisos de versiones en japonés
EXCLUIR_JAPONES = False
JAPONES_RE = re.compile(r"japan|japon|\bjp\b|\bjap\b", re.I)
DESCUENTO_RE = re.compile(r"-\s?\d{1,2}\s?%\s*(?:" + PRECIO_RE.pattern + ")")

# Filtro de producto sellado (ponlo en False si quieres avisos también de cartas sueltas)
SOLO_SELLADO = True
# Lo que SÍ interesa: cajas/displays/cases/sobres, premium y colecciones, latas y mini case sets
SELLADO_RE = re.compile(
    r"booster|box|display|\bcase\b|caja|sobre|collection|colecci|premium|illustration|"
    r"\btin\b|latas?\b|gift|bundle|set\s*sail|sellad|sealed|precintad",
    re.I,
)
# Lo que NO interesa: starter decks, double packs y mazos (salvo el Set Sail Deck)
STARTER_RE = re.compile(
    r"starter|double\s*pack|\bdp[\s\-]?\d{1,2}\b|\bst[\s\-]?\d{1,3}\b|mazo|baraja|\bdeck\b|pack\s+decks?",
    re.I,
)
# Código de carta suelta tipo OP05-060 / ST01-012 (los sets sellados son OP-20, ST-44... sin dígitos antes del guion)
CARTA_RE = re.compile(r"\b(?:OP|EB|ST|PRB|P)\d{1,2}-\d{2,3}\b", re.I)

# Para entrar en la categoría de One Piece cuando el buscador solo devuelve enlaces a categorías
PRODUCTO_URL_RE = re.compile(
    r"/producto/|/product/|/products/|/\d+-[^/]+\.html$|-p-\d+-\d+-\d+/?$|id_product=", re.I
)
EXCLUIR_CAT_RE = re.compile(
    r"manga|funko|\bpop\b|figura|peluche|camiseta|ropa|libro|anime|llavero|sleeve|funda|"
    r"protector|playmat|album|carpeta|accesorio|merch",
    re.I,
)
# Accesorios y merchandising que no interesan aunque lleven "box" o "set" en el nombre
EXCLUIR_PRODUCTO_RE = re.compile(
    r"playmat|sleeve|funda|protector|storage|deck\s?box|binder|carpeta|album|[áa]lbum|figura|figure|"
    r"peluche|p[óo]ster|camiseta|llavero|mousepad|alfombrilla|tapete|inscripci|hucha|estandarte|"
    r"blind\s*box|battle\s*record|memories of adventure|presentaci[óo]n|torneo|evento|release event|prerelease",
    re.I,
)
# Textos de etiquetas (descuentos, "añadir al carrito"...) que no son el nombre del producto
BADGE_RE = re.compile(
    r"^\s*(?:-?\d+\s*%|¡|añadir|comprar|ver\b|oferta|nuevo\b|new\b|agotad|sold|[\W\d_]*$)", re.I
)
# Si el buscador de una tienda no sirve, aquí puedes poner una ruta de búsqueda propia.
RUTAS_EXTRA = {
    "comicstores.es": ["/busqueda/listaLibros.php?tipoBus=full&palabrasBusqueda=one+piece+card+game"],
}

CARD_SEL = ("li.product, article.product, article.product-miniature, li.ajax_block_product, "
            "div.product-miniature, div.product, .product-grid-item, .js-product")


def get(url, **kw):
    r = S.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def clean(url):
    return url.split("#")[0].split("?")[0]


def nombre(d):
    if d in NOMBRES:
        return NOMBRES[d]
    partes = d.split(".")
    return (partes[-2] if len(partes) > 1 else d).replace("-", " ").title()


def fmt_precio(x):
    """160.0 -> '160,00 €'  /  2800 -> '2.800,00 €'"""
    try:
        s = f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return None
    return s.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def a_precio(num):
    return f"{num} €" if "," in num else fmt_precio(num)


def a_float(num):
    return float(num.replace(".", "").replace(",", ".")) if "," in num else float(num)


def buscar_precio(card):
    for d in card.select("del, .regular-price, .old-price, s"):  # precios tachados
        d.extract()
    texto = card.get_text(" ", strip=True)
    m = DESCUENTO_RE.search(texto)  # "221,99€ -10% 199,79€": vale el que va tras el descuento
    if m:
        return a_precio(m.group(1) or m.group(2))
    for el in card.select(".price, .product-price, .woocommerce-Price-amount, [itemprop=price]"):
        m = PRECIO_RE.search(el.get_text(" ", strip=True))
        if m:
            return a_precio(m.group(1) or m.group(2))
    m = PRECIO_RE.search(texto)
    return a_precio(m.group(1) or m.group(2)) if m else None


def limpiar_titulo(t):
    """Quita HTML, precio, descuento y estado que algunas webs pegan al nombre. Devuelve (titulo, truncado)."""
    t = html.unescape(t).replace("&#", "–")
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]*>?", " ", t)).strip()
    t = re.split(r"\s+\d[\d.,]*\s?€", t, maxsplit=1)[0]
    truncado = ". . ." in t or "…" in t or t.endswith("...")
    t = t.replace(" . . . ", " … ").replace(". . .", "…").strip(" -–")
    return t, truncado


def ficha(url):
    """Lee la página del producto (solo al avisar): devuelve (titulo_completo, precio); None si no lo encuentra."""
    try:
        soup = BeautifulSoup(get(url).text, "html.parser")
    except Exception:
        return None, None
    h = soup.select_one("h1.product_title, h1.product-title, h1[itemprop=name], h1")
    titulo = h.get_text(" ", strip=True) if h else None
    precio = None
    meta = soup.select_one('meta[property="product:price:amount"], meta[itemprop=price]')
    if meta and meta.get("content"):
        precio = fmt_precio(meta["content"])
    if not precio:
        for el in soup.select(".price, .product-price, [itemprop=price], .current-price"):
            m = PRECIO_RE.search(el.get_text(" ", strip=True))
            if m:
                precio = a_precio(m.group(1) or m.group(2))
                break
    return titulo, precio


# ---------------------------------------------------------------- lectura de HTML

def extraer(soup, base, exigir_op=True, token=None):
    """Saca {url: {titulo, disponible, precio, carrito}} de una página de listado."""
    out = {}
    origen = "{0.scheme}://{0.netloc}".format(urlsplit(base))
    for card in soup.select(CARD_SEL):
        anclas = (card.select("h1 a, h2 a, h3 a, h4 a, .product-title a, .product-name a")
                  or card.select("a.woocommerce-LoopProduct-link") or card.select("a[href]"))
        anclas = [x for x in anclas if x.get("href") and not x["href"].startswith(("#", "javascript"))]
        if not anclas:
            continue

        def texto_a(x):
            return (x.get("title") or x.get_text(" ", strip=True) or "").strip()

        buenas = [x for x in anclas if not BADGE_RE.match(texto_a(x))]
        a = max(buenas or anclas, key=lambda x: len(texto_a(x)))
        link = clean(urljoin(base, a["href"]))
        titulo = texto_a(a)
        if not titulo or BADGE_RE.match(titulo):
            h = card.select_one("h1, h2, h3, h4")
            titulo = h.get_text(" ", strip=True) if h else link
        titulo, corto = limpiar_titulo(titulo)
        if titulo.endswith(("...", "…")):  # título cortado: usar el nombre que va en la URL
            slug = re.sub(r"\.html?$", "", urlsplit(link).path.rstrip("/").split("/")[-1])
            slug = re.sub(r"^\d+-", "", slug).replace("-", " ").strip()
            titulo = slug or titulo
        if link in out or (exigir_op and not OP_RE.search(titulo + " " + link)):
            continue
        marca = " ".join(card.get("class", [])) + " " + card.get_text(" ", strip=True)
        carrito = None
        c = card.select_one('a[href*="add-to-cart="], a[href*="add=1"], a[href*="cart/add"]')
        if c:
            carrito = urljoin(base, c["href"])
        else:
            hid = card if card.get("data-id-product") else card.select_one("[data-id-product]")
            if hid is not None and token:
                carrito = (f"{origen}/index.php?controller=cart&add=1&id_product={hid['data-id-product']}"
                           f"&id_product_attribute=0&token={token}")
        out[link] = {
            "titulo": titulo,
            "disponible": not AGOTADO_RE.search(marca),
            "precio": buscar_precio(card),
            "carrito": carrito,
            **({"corto": True} if corto else {}),
        }
    if not out:  # sin tarjetas reconocibles: enlaces sueltos (fuera de menús y pie de página)
        for a in soup.find_all("a", href=True):
            if a.find_parent(["nav", "header", "footer"]):
                continue
            link = clean(urljoin(base, a["href"]))
            titulo = a.get_text(" ", strip=True)
            if link in out or len(titulo) < 8 or BADGE_RE.match(titulo):
                continue
            if exigir_op:
                if not ONE_RE.search(link + " " + titulo):
                    continue
            elif not PRODUCTO_URL_RE.search(link):
                continue
            padre = a.parent.parent if a.parent and a.parent.parent else a
            out[link] = {
                "titulo": titulo,
                "disponible": not AGOTADO_RE.search(padre.get_text(" ", strip=True)),
                "precio": None,
                "carrito": None,
            }
    return out


PAG_RE = re.compile(r"(?:/page/|[?&](?:paged|page|pag|p)=)(\d+)", re.I)


def siguiente_pagina(soup, base, actual):
    """URL de la página siguiente del listado (por 'siguiente' o por el enlace a /page/N+1)."""
    nxt = soup.select_one("a[rel=next], a.next, li.next a, .pagination .next a")
    if nxt and nxt.get("href"):
        return urljoin(base, nxt["href"])
    for a in soup.find_all("a", href=True):
        m = PAG_RE.search(a["href"])
        if m and int(m.group(1)) == actual + 1:
            return urljoin(base, a["href"])
    return None


def candidatas_pagina(url, n):
    """Direcciones probables de la página n de un listado (/page/n/ de WordPress o ?page=n)."""
    sp = urlsplit(url)
    c = []
    path = re.sub(r"/page/\d+/?$", "/", sp.path)
    if path.endswith("/"):
        c.append(sp._replace(path=f"{path}page/{n}/").geturl())
    q = [x for x in sp.query.split("&") if x and not re.match(r"(page|paged)=", x)]
    c.append(sp._replace(query="&".join(q + [f"page={n}"])).geturl())
    return c


def paginar(url, exigir_op=True, max_paginas=8):
    """Lee un listado y todas sus páginas. Devuelve (productos, ultimo_error)."""
    out, vistas, error = {}, set(), None
    cands = [url]
    for actual in range(1, max_paginas + 1):
        r = None
        for cu in cands:
            if cu in vistas:
                continue
            vistas.add(cu)
            try:
                r = get(cu)
                break
            except Exception as e:
                error = e
        if r is None:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        m = re.search(r'static_token["\']?\s*[:=]\s*["\']([0-9a-f]{32})', r.text)
        nuevos = 0
        for k, v in extraer(soup, r.url, exigir_op, m.group(1) if m else None).items():
            if k not in out:
                out[k] = v
                nuevos += 1
        if actual > 1 and nuevos == 0:  # página repetida o vacía: se acabó
            break
        nxt = siguiente_pagina(soup, r.url, actual)
        if nxt:
            cands = [nxt]
        elif nuevos >= 6:  # página llena y sin enlace "siguiente": probar /page/N/ y ?page=N
            cands = candidatas_pagina(r.url, actual + 1)
        else:
            break
        time.sleep(0.5)
    return out, error


# ---------------------------------------------------------------- estrategias

def shopify(d):
    out = {}
    for page in range(1, 21):
        prods = get(f"https://{d}/products.json", params={"limit": 250, "page": page}).json().get("products", [])
        if not prods:
            break
        for p in prods:
            tags = p.get("tags", "")
            if isinstance(tags, list):
                tags = " ".join(tags)
            texto = " ".join([p.get("title", ""), p.get("handle", ""), tags, p.get("product_type", "")])
            if ONE_RE.search(texto):
                url = f"https://{d}/products/{p['handle']}"
                vs = p.get("variants", [])
                v = next((x for x in vs if x.get("available")), vs[0] if vs else {})
                out[url] = {
                    "titulo": p.get("title", ""),
                    "disponible": any(x.get("available") for x in vs),
                    "precio": fmt_precio(v.get("price")) if v.get("price") else None,
                    "carrito": f"https://{d}/cart/add?id={v['id']}&quantity=1" if v.get("id") else None,
                }
        if len(prods) < 250:
            break
        time.sleep(0.5)
    return out


def woocommerce(d):
    out = {}
    try:
        for page in range(1, 6):
            data = get(
                f"https://{d}/wp-json/wc/store/v1/products",
                params={"search": "one piece", "per_page": 100, "page": page},
            ).json()
            if not isinstance(data, list):
                raise ValueError("no es la Store API")
            for p in data:
                titulo = html.unescape(p.get("name", ""))
                url = clean(p.get("permalink", ""))
                if url and OP_RE.search(titulo + " " + url):
                    pr = p.get("prices") or {}
                    precio = None
                    if pr.get("price"):
                        try:
                            precio = fmt_precio(int(pr["price"]) / 10 ** int(pr.get("currency_minor_unit", 2)))
                        except (TypeError, ValueError):
                            precio = None
                    out[url] = {
                        "titulo": titulo,
                        "disponible": bool(p.get("is_in_stock")) and bool(p.get("is_purchasable", True)),
                        "precio": precio,
                        "carrito": f"{url}?add-to-cart={p['id']}" if p.get("id") else None,
                    }
            if len(data) < 100:
                break
        if out:
            return out
    except Exception:
        pass
    # Si la Store API está desactivada, buscador HTML normal de WordPress
    return html_search(d, ["/?s=one+piece&post_type=product"])


def html_search(d, paths):
    """Abre el buscador de la tienda y lee las tarjetas de producto."""
    ultimo = None
    for path in paths:
        out, err = paginar(f"https://{d}{path}")
        ultimo = err or ultimo
        if out:
            return out
    if ultimo:
        raise ultimo
    return {}


def html_categoria(d):
    """Busca en la tienda los enlaces a la categoría de One Piece y lee los productos de dentro."""
    candidatos = []
    for path in ["/?s=one+piece&post_type=product", "/"]:
        try:
            r = get(f"https://{d}{path}")
        except Exception:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url, a["href"]).split("#")[0]
            texto = a.get_text(" ", strip=True)
            if (ONE_RE.search(href + " " + texto)
                    and not PRODUCTO_URL_RE.search(href)
                    and not EXCLUIR_CAT_RE.search(href + " " + texto)
                    and urlsplit(href).netloc.replace("www.", "") == d
                    and href not in candidatos):
                candidatos.append(href)
        if candidatos:
            break
    if DEBUG:
        print("    categorías candidatas:", [c[-60:] for c in candidatos[:6]])
    out = {}
    for url in candidatos[:6]:
        items, _ = paginar(url, exigir_op=False)
        out.update(items)
    return out


def prestashop(d):
    return html_search(d, [
        "/index.php?controller=search&s=one+piece&resultsPerPage=100",
        "/buscar?controller=search&s=one+piece",
        "/search?controller=search&s=one+piece",
    ])


def generico(d):
    return html_search(d, [
        "/search?q=one+piece", "/?s=one+piece", "/buscar?s=one+piece",
        "/index.php?controller=search&s=one+piece",
    ])


def rutas_extra(d):
    return html_search(d, RUTAS_EXTRA[d]) if d in RUTAS_EXTRA else {}


CADENA = {
    "shopify": [shopify, html_categoria, rutas_extra],
    "woocommerce": [woocommerce, html_categoria, rutas_extra],
    "prestashop": [prestashop, html_categoria, generico, rutas_extra],
    "auto": [shopify, woocommerce, prestashop, html_categoria, generico, rutas_extra],
}


def solo_sellado(items):
    """Descarta cartas sueltas: se queda con cajas, mazos, packs, colecciones..."""
    if not SOLO_SELLADO:
        return items
    return {
        u: i for u, i in items.items()
        if SELLADO_RE.search(i["titulo"] + " " + u) and not CARTA_RE.search(i["titulo"] + " " + u)
        and len(i["titulo"].strip()) >= 12  # descarta enlaces de categoría tipo "One Piece"
        and not EXCLUIR_PRODUCTO_RE.search(i["titulo"])
        and not (EXCLUIR_JAPONES and JAPONES_RE.search(i["titulo"] + " " + u))
        and (not STARTER_RE.search(i["titulo"] + " " + u) or re.search(r"set\s*sail", i["titulo"] + " " + u, re.I))
    }


def consultar(d, plataforma):
    errores = []
    for f in CADENA[plataforma]:
        try:
            items = solo_sellado(f(d))
            if items:
                return d, plataforma, items, None
            errores.append(f"{f.__name__}: sin resultados")
        except Exception as e:
            errores.append(f"{f.__name__}: {e}")
    return d, plataforma, None, "; ".join(errores)


# ---------------------------------------------------------------- avisos

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def patron_repos(marcas):
    """'repone sobre todo sáb/dom 8-11h (7 reposiciones)' a partir de las reposiciones vistas."""
    fechas = []
    for m in marcas:
        try:
            fechas.append(datetime.fromisoformat(m))
        except ValueError:
            pass
    if len(fechas) < 3:
        return ""
    cd = Counter(f.weekday() for f in fechas)
    mx = max(cd.values())
    dias = sorted(k for k, c in cd.items() if c >= 2 and c >= 0.6 * mx)
    ch = Counter(f.hour for f in fechas)
    ini = max(range(22), key=lambda h: (ch[h] + ch[h + 1] + ch[h + 2], -h))
    if not dias or ch[ini] + ch[ini + 1] + ch[ini + 2] < 2:
        return ""
    return f"repone sobre todo {'/'.join(DIAS[k] for k in dias)} {ini}-{ini + 3}h ({len(fechas)} reposiciones)"


def mensaje(d, tipo, url, it, marcas_repos):
    """Aviso en el formato del bot: tipo = 'repo' (reposición) o 'nuevo'."""
    nom, disp = nombre(d), it["disponible"]
    titulo, precio = it["titulo"], it.get("precio")
    if it.get("corto") or not precio:
        t2, p2 = ficha(url)
        if it.get("corto") and t2 and len(t2) > 8:
            titulo = t2
        precio = precio or p2
    it = {**it, "titulo": titulo, "precio": precio}
    l = [f"🟢 Hay stock · {nom}" if tipo == "repo" else f"🆕 Nuevo producto · {nom}",
         f"📦 {titulo}"]
    if it.get("precio"):
        l.append(f"💰 {it['precio']}")
    l += ["", "🛍️ Disponible: ❌ → ✅" if tipo == "repo" else f"🛍️ Disponible: {'✅' if disp else '❌'}", "",
          f"🔗 {url}"]
    if it.get("carrito"):
        l.append(f"🛒 Añadir al carrito: {it['carrito']}")
    l.append("✅ Entra ya: deja añadir al carrito." if disp else "👉 Entra ya: puede haber stock.")
    if PREVENTA_RE.search(it["titulo"] + " " + url):
        l.append("📅 Es una reserva/preventa: se paga ahora y llega cuando salga.")
    if d.endswith(".de"):
        l.append(f"🇩🇪 Tienda alemana (https://{d}): envía desde Alemania, web en alemán.")
    pat = patron_repos(marcas_repos)
    if pat:
        l.append(f"🕘 {nom} {pat}")
    corto = f"{'🔔' if tipo == 'repo' else '🆕'} [{nom}] {it['titulo']}\n{url}"
    return "\n".join(l), corto


def enviar(texto):
    print(texto + "\n")
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "disable_web_page_preview": "true"},
                timeout=30,
            )
        except Exception as e:
            print(f"(no se pudo enviar a Telegram: {e})", file=sys.stderr)


def avisar(eventos):
    """eventos = [(mensaje_completo, resumen)]. Un mensaje por producto; si hay muchos, el resto va resumido."""
    for largo, _ in eventos[:MAX_MENSAJES]:
        enviar(largo)
        time.sleep(1)
    bloque = ""
    for _, corto in eventos[MAX_MENSAJES:]:
        if bloque and len(bloque) + len(corto) + 2 > 3800:
            enviar(bloque.strip())
            bloque = ""
        bloque += corto + "\n\n"
    if bloque.strip():
        enviar(bloque.strip())


# ---------------------------------------------------------------- diagnóstico y main

def diagnostico(d):
    """Muestra qué ve el script en una tienda (para ajustar las que fallan)."""
    global DEBUG
    DEBUG = True
    plat = next((p for p, ds in STORES.items() if d in ds), "auto")
    print(f"\n===== {d} ({plat})")
    for f in CADENA[plat]:
        try:
            raw = f(d)
            keep = solo_sellado(raw)
            print(f"{f.__name__}: {len(raw)} encontrados, {len(keep)} tras filtro sellado")
            for u, i in list(raw.items())[:6]:
                print("   ", "ok       " if u in keep else "DESCARTADO", i["titulo"][:60], "|",
                      i.get("precio"), "|", "carrito" if i.get("carrito") else "sin carrito", "|", u[-45:])
        except Exception as e:
            print(f"{f.__name__}: ERROR {str(e)[:150]}")
    for path in ["/wp-json/wc/store/v1/products?search=one+piece&per_page=2",
                 "/index.php?controller=search&s=one+piece", "/?s=one+piece&post_type=product",
                 "/search?q=one+piece"]:
        try:
            r = get(f"https://{d}{path}")
            if "json" in r.headers.get("content-type", ""):
                print(f"GET {path} -> {r.status_code} JSON: {r.text[:160]!r}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            enl = [a for a in soup.find_all("a", href=True) if ONE_RE.search(a["href"] + " " + a.get_text(" "))]
            print(f"GET {path} -> {r.status_code}, {len(r.text)} bytes, tarjetas={len(soup.select(CARD_SEL))}, "
                  f"enlaces one-piece={len(enl)}, url final={r.url[:70]}")
            for a in enl[:3]:
                print("     ", a.get_text(" ", strip=True)[:50], "|", a["href"][:80])
        except Exception as e:
            print(f"GET {path} -> ERROR {str(e)[:110]}")


def main():
    if "--diag" in sys.argv:
        for d in sys.argv[sys.argv.index("--diag") + 1:]:
            diagnostico(d)
        return
    tiendas = [(d, p) for p, ds in STORES.items() for d in ds]
    with ThreadPoolExecutor(HILOS) as ex:
        resultados = list(ex.map(lambda t: consultar(*t), tiendas))

    if "--probar" in sys.argv:
        for d, p, items, err in sorted(resultados, key=lambda r: (r[3] is not None, r[0])):
            if err:
                print(f"FALLO  {d:28} ({p}) {err[:110]}")
            else:
                disp = sum(1 for v in items.values() if v["disponible"])
                print(f"OK     {d:28} ({p}) {len(items)} productos, {disp} disponibles")
        return

    estado = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    tiendas_est = estado.setdefault("tiendas", {})
    fallos = estado.setdefault("fallos", {})
    repos = estado.setdefault("repos", {})
    eventos = []

    for d, p, items, err in resultados:
        if err or not items:
            fallos[d] = fallos.get(d, 0) + 1
            print(f"[{d}] fallo ({fallos[d]}): {err}", file=sys.stderr)
            if fallos[d] == MAX_FALLOS:
                aviso = f"⚠️ {d} lleva {MAX_FALLOS} ejecuciones seguidas sin responder. Revisa la tienda."
                eventos.append((aviso, aviso))
            continue
        fallos[d] = 0
        previo = tiendas_est.get(d)
        if previo is None:  # primera vez con esta tienda: solo guardamos la base, sin avisar
            tiendas_est[d] = items
            print(f"[{d}] base inicial: {len(items)} productos")
            continue
        for url, it in items.items():
            if url not in previo:
                eventos.append(mensaje(d, "nuevo", url, it, repos.get(d, [])))
            elif it["disponible"] and not previo[url]["disponible"]:
                marcas = repos.setdefault(d, [])
                marcas.append(datetime.now().isoformat(timespec="minutes"))
                del marcas[:-200]
                eventos.append(mensaje(d, "repo", url, it, marcas))
        # fusionar para no perder productos si una consulta devuelve resultados incompletos
        tiendas_est[d] = {**previo, **items}

    if eventos:
        avisar(eventos)

    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


if __name__ == "__main__":
    main()
