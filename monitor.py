import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# LIBRERÍA CLAVE: Imita la huella digital TLS de Chrome (con fallback seguro)
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

# Mantenemos requests estándar para Telegram y reintentos
import requests 
import urllib3
from requests.adapters import HTTPAdapter
from ddgs import DDGS


# ============================================================
# RUTAS Y ARCHIVOS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
CONFIG_FILE = Path(os.getenv("CONFIG_PATH", os.getenv("CONFIG_FILE", DATA_DIR / "config.json")))
STATE_FILE = Path(os.getenv("STATE_PATH", os.getenv("STATE_FILE", DATA_DIR / "monitor_state.json")))
HISTORY_FILE = Path(os.getenv("HISTORY_PATH", os.getenv("HISTORY_FILE", DATA_DIR / "historico.csv")))

TERMINOS_EXCLUSION_DEFECTO = [
    "reacondicionado", "refurbished", "recertified",
    "segunda mano", "usado", "used", "renewed",
    "cable", "carcasa", "caddy", "funda", "dock", "adaptador"
]

COLUMNAS_CSV = [
    "fecha",
    "producto_id",
    "producto",
    "tienda",
    "pais",
    "precio_producto",
    "envio",
    "envio_confirmado",
    "total_unidad_con_envio",
    "cantidad",
    "total_pedido_con_envio",
    "precio_unidad_medida",
    "cumple_objetivo",
    "url",
]

# ============================================================
# USER-AGENTS ROTATORIOS Y HEADERS REALISTAS
# ============================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

BASE_HEADERS = {
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8,de;q=0.7,fr;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

def get_robust_session():
    """Crea una sesión de requests con reintentos exponenciales y forzando HTTP/1.1"""
    session = requests.Session()
    # Configuración de reintentos: 3 intentos, esperando 2s, 4s, 8s (backoff exponencial)
    retry_strategy = urllib3.util.Retry(
        total=2,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 504], #503,
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# ============================================================
# CARGA DE CONFIGURACION Y ESTADO
# ============================================================

def cargar_configuracion():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Unificación: solo_union_europea es la clave maestra. 
    # Si existe la antigua, se ignora o se mapea por compatibilidad.
    config.setdefault("solo_union_europea", True)
    if "buscar_en_europa" in config and "solo_union_europea" not in config:
        config["solo_union_europea"] = config["buscar_en_europa"]
        
    config.setdefault("pais", "España")
    config.setdefault("moneda", "EUR")
    config.setdefault("tasa_pln_eur", 0.235)  # 1 PLN ≈ 0.235 EUR (Polonia / UE)
    config.setdefault("incluir_wallapop_vinted", True)
    config.setdefault("ignorar_vendidos", True) # Nueva clave explícita
    config.setdefault("envio_wallapop_vinted_estimado", 4.50)
    config.setdefault("envio_europa_estimado", 6.50)
    config.setdefault("incluir_envio", True)
    config.setdefault("exigir_envio_confirmado", False)
    config.setdefault("envio_por_defecto", 4.50)
    config.setdefault("notificar_bajadas", True)
    config.setdefault("solo_nuevas_ofertas", True)
    config.setdefault("excluir_terminos", list(TERMINOS_EXCLUSION_DEFECTO))
    config.setdefault("productos", [])

    return config


def cargar_estado():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                datos = json.load(f)
                if isinstance(datos, dict):
                    datos.setdefault("ofertas", {})
                    return datos
        except (OSError, json.JSONDecodeError):
            print("Estado anterior inválido. Se inicia uno nuevo.")

    return {"ofertas": {}}


def guardar_estado(estado):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporal = STATE_FILE.with_suffix(".tmp")
    try:
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)
        try:
            temporal.replace(STATE_FILE)
        except OSError:
            # En contenedores Docker con bind-mount de archivo (- ./monitor_state.json:/app/monitor_state.json),
            # Linux devuelve Errno 16 (Device or resource busy) al intentar renombrar sobre un mount point.
            # En ese caso, escribimos directamente sobre el archivo montado.
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(estado, f, indent=2, ensure_ascii=False)
            if temporal.exists():
                temporal.unlink()
    except Exception as e:
        print(f"⚠️ Error al guardar estado: {e}")


# ============================================================
# EXTRACCION Y PARSEO DE NUMEROS, PRECIOS Y ENVIOS
# ============================================================

TASA_PLN_A_EUR = 0.235  # Tipo de cambio medio aproximado 1 PLN ≈ 0.235 EUR (1 EUR ≈ 4.25 PLN)

def convertir_numero(texto):
    if isinstance(texto, (int, float)):
        return float(texto)
    texto = str(texto).replace("\u00a0", " ").replace("&nbsp;", " ")
    # Extraer el primer bloque numérico limpio (soporta 1.234,56 o 1234.56 o 179)
    m = re.search(r"(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?)", texto)
    if not m:
        raise ValueError(f"No se encontró un número válido en: {texto}")
    num_str = m.group(1).strip()
    if "," in num_str and "." in num_str:
        if num_str.rfind(",") > num_str.rfind("."):
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif "," in num_str:
        num_str = num_str.replace(",", ".")
    return float(num_str)


def extraer_precio(texto):
    if not texto:
        return None

    # Desescapar entidades HTML (como &nbsp;, &#8364;, etc.)
    texto_unescaped = html.unescape(texto)

    # Monedas permitidas al final de expresiones: € o eur o euros
    moneda = r"(?:€|eur(?:os)?)"

    # 1. ELIMINAR FALSOS POSITIVOS DE FILTROS LATERALES Y RANGOS
    texto_limpio = re.sub(
        rf"\b(?:under|moins de|sous|menos de|unter|less than|sotto|gifts under|regalos por debajo de|cadeaux de moins de)\s+\d+(?:[.,]\d+)?\s*{moneda}(?!\w)", 
        "", 
        texto_unescaped, 
        flags=re.IGNORECASE
    )

    # 2. ELIMINAR CUOTAS Y PRECIOS DE FINANCIACIÓN POR MES (ej. "21,60 €/mes", "cuotas de 15 €")
    texto_limpio = re.sub(
        rf"\b(?:desde\s+)?\d+(?:[.,]\d+)?\s*{moneda}?\s*(?:/|\s*al\s*|\s*por\s*|\s*cada\s*)?(?:mes|m|month|monat)\b",
        "",
        texto_limpio,
        flags=re.IGNORECASE
    )
    texto_limpio = re.sub(
        rf"\b\d+\s*(?:cuotas?|plazos?|meses)\s*(?:de)?\s*\d+(?:[.,]\d+)?\s*{moneda}(?!\w)",
        "",
        texto_limpio,
        flags=re.IGNORECASE
    )
    texto_limpio = re.sub(
        rf"\b(?:cuota|plazo|financiaci[oó]n)(?:\s+desde)?\s*:\s*\d+(?:[.,]\d+)?\s*{moneda}(?!\w)",
        "",
        texto_limpio,
        flags=re.IGNORECASE
    )

    # 3. ELIMINAR AHORROS Y DESCUENTOS (ej. "ahorra 216 €", "descuento de 50 €")
    texto_limpio = re.sub(
        rf"\b(?:ahorra|ahorro|descuento|rebaja|save|rabatt|remise|économisez)\s*(?:de|hasta|up to|jusqu'à)?\s*\d+(?:[.,]\d+)?\s*{moneda}(?!\w)",
        "",
        texto_limpio,
        flags=re.IGNORECASE
    )
    texto_limpio = re.sub(
        rf"\b\d+(?:[.,]\d+)?\s*{moneda}\s*(?:de\s+)?(?:descuento|ahorro|rebaja|de remise)\b",
        "",
        texto_limpio,
        flags=re.IGNORECASE
    )

    # 4. ELIMINAR PRECIOS TACHADOS / ANTERIORES (ej. "Antes: 520,00 €", "PVP: 450 €")
    texto_limpio = re.sub(
        rf"\b(?:antes|pvp|precio anterior|precio recomendado|was|statt|au lieu de)\s*:\s*\d+(?:[.,]\d+)?\s*{moneda}(?!\w)",
        "",
        texto_limpio,
        flags=re.IGNORECASE
    )

    # 5. Schema.org JSON-LD (Amazon, MediaMarkt y tiendas europeas estándar)
    for m in re.finditer(r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>', texto_limpio, re.DOTALL | re.IGNORECASE):
        try:
            datos_ld = json.loads(m.group(1))
            bloques = []
            if isinstance(datos_ld, list):
                bloques.extend(datos_ld)
            elif isinstance(datos_ld, dict):
                if "@graph" in datos_ld and isinstance(datos_ld["@graph"], list):
                    bloques.extend(datos_ld["@graph"])
                else:
                    bloques.append(datos_ld)
            for b in bloques:
                if not isinstance(b, dict):
                    continue
                offers = b.get("offers")
                if offers:
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict):
                        avail = str(offers.get("availability", "")).lower()
                        if any(s in avail for s in ["outofstock", "soldout", "discontinued"]):
                            return None
                        val = offers.get("price") or offers.get("lowPrice")
                        if val is not None:
                            precio_ld = convertir_numero(val)
                            curr = str(offers.get("priceCurrency", "")).upper()
                            if curr == "PLN":
                                precio_ld = round(precio_ld * TASA_PLN_A_EUR, 2)
                            if 1 <= precio_ld <= 100000:
                                return precio_ld
        except Exception:
            pass

    # 6. Meta tags OpenGraph / Twitter / Product (ej. <meta property="og:price:amount" content="422.50">)
    m_meta = re.search(
        r'<meta[^>]+(?:property|name)=[\'"][^\'"]*(?:price:amount|data1|price)[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
        texto_limpio,
        re.IGNORECASE
    ) or re.search(
        r'<meta[^>]+content=[\'"]([^\'"]+)[\'"][^>]+(?:property|name)=[\'"][^\'"]*(?:price:amount|data1|price)[\'"]',
        texto_limpio,
        re.IGNORECASE
    )
    if m_meta:
        try:
            p = convertir_numero(m_meta.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # 6B. Microdata itemprop="price" (PrestaShop, Informática Barata, Shop Duty Free, WooCommerce, etc.)
    m_itemprop = re.search(
        r'<[^>]+itemprop=[\'"]price[\'"][^>]*content=[\'"]([^\'"]+)[\'"]',
        texto_limpio,
        re.IGNORECASE
    ) or re.search(
        r'<[^>]+content=[\'"]([^\'"]+)[\'"][^>]*itemprop=[\'"]price[\'"]',
        texto_limpio,
        re.IGNORECASE
    )
    if m_itemprop:
        try:
            p = convertir_numero(m_itemprop.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    m_itemprop_txt = re.search(
        r'<[^>]+itemprop=[\'"]price[\'"][^>]*>([^<]+)<',
        texto_limpio,
        re.IGNORECASE
    )
    if m_itemprop_txt:
        try:
            p = convertir_numero(m_itemprop_txt.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # 7. SELECTORES HTML ESPECÍFICOS DE AMAZON
    # A) Buy Box principal de Amazon (apexPriceToPay / corePriceDisplay / priceToPay)
    m_amazon_buybox = re.search(
        r'class=[\'"][^\'"]*(?:apex-pricetopay-value|priceToPay|corePriceDisplay)[^\'"]*[\'"][^>]*>.*?<span[^>]*class=[\'"][^\'"]*a-offscreen[^\'"]*[\'"][^>]*>([^<]+)</span>',
        texto_limpio,
        re.DOTALL | re.IGNORECASE
    )
    if m_amazon_buybox and m_amazon_buybox.group(1).strip():
        try:
            p = convertir_numero(m_amazon_buybox.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # B) Datos ocultos del producto principal en Amazon (twister-plus / customerVisiblePrice)
    m_amazon_input = re.search(
        r'<input[^>]+(?:id=[\'"]twister-plus-price-data-price[\'"]|name=[\'"][^\'"]*customerVisiblePrice\[amount\][\'"])[^>]+value=[\'"]([\d.,]+)[\'"]',
        texto_limpio,
        re.IGNORECASE
    )
    if m_amazon_input:
        try:
            p = convertir_numero(m_amazon_input.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # C) <span class="a-offscreen">179,00 €</span>
    for m in re.finditer(r'<span[^>]*class=[\'"][^\'"]*a-offscreen[^\'"]*[\'"][^>]*>([^<]+)</span>', texto_limpio, re.IGNORECASE):
        try:
            p = convertir_numero(m.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # B) <span class="a-price-whole">179<span class="a-price-decimal">,</span></span><span class="a-price-fraction">00</span>
    m_whole = re.search(
        r'<span[^>]*class=[\'"][^\'"]*a-price-whole[^\'"]*[\'"][^>]*>([\d.,]+)[^<]*(?:<span[^>]*>[^<]*</span>)?</span>\s*<span[^>]*class=[\'"][^\'"]*a-price-fraction[^\'"]*[\'"][^>]*>(\d{2})</span>',
        texto_limpio,
        re.IGNORECASE
    )
    if m_whole:
        try:
            entero = m_whole.group(1).replace(".", "").replace(",", "")
            fraccion = m_whole.group(2)
            p = float(f"{entero}.{fraccion}")
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # 8. MediaMarkt selector branded-price
    m_mm = re.search(r'<span[^>]*data-test=[\'"]branded-price[\'"][^>]*>([^<]+)</span>', texto_limpio, re.IGNORECASE)
    if m_mm:
        try:
            p = convertir_numero(m_mm.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # 9. JSON INTERNO (Amazon / MediaMarkt / e-commerce)
    json_price = re.search(
        r'[\'"](?:priceToPay|buyingPrice|priceAmount|currentPrice|salesPrice)[\'"]\s*:\s*\{?[^}]*?[\'"]?(?:amount|value)?[\'"]?\s*:\s*[\'"]?([\d.,]+)[\'"]?',
        texto_limpio,
        re.IGNORECASE
    )
    if json_price:
        try:
            p = convertir_numero(json_price.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    json_price_2 = re.search(r'"price_string"\s*:\s*"([^"]+)"', texto_limpio, re.IGNORECASE)
    if json_price_2:
        try:
            p = convertir_numero(json_price_2.group(1))
            if 1 <= p <= 100000:
                return p
        except ValueError:
            pass

    # 9B. JSON Next.js __NEXT_DATA__ (Wallapop, Vinted y tiendas SPA)
    m_next = re.search(r"""<script[^>]*id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""", texto_limpio, re.DOTALL | re.IGNORECASE)
    if m_next:
        m_cash = re.search(r"""["'](?:cash|price)["']\s*:\s*\{?[^}]*?["']amount["']\s*:\s*([\d.]+)""", m_next.group(1))
        if m_cash:
            try:
                p = float(m_cash.group(1))
                if 1 <= p <= 100000:
                    return p
            except ValueError:
                pass

    # 10. PATRONES REGEX GENERALES (para resúmenes DDG y texto plano)
    patrones = [
        r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*€",
        r"€\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)",
        r"(\d+(?:[.,]\d{1,2})?)\s*(?:EUR|euros?)\b",
    ]
    for patron in patrones:
        for coincidencia in re.finditer(patron, texto_limpio, re.IGNORECASE):
            try:
                precio = convertir_numero(coincidencia.group(1))
                if 1 <= precio <= 100000:
                    return precio
            except ValueError:
                continue

    # 11. PATRONES REGEX PARA POLONIA (PLN / złoty convertido a EUR)
    patrones_pln = [
        r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*(?:zł|zl|pln)\b",
        r"(?:zł|zl|pln)\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)",
    ]
    for patron in patrones_pln:
        for coincidencia in re.finditer(patron, texto_limpio, re.IGNORECASE):
            try:
                precio_pln = convertir_numero(coincidencia.group(1))
                precio_eur = round(precio_pln * TASA_PLN_A_EUR, 2)
                if 1 <= precio_eur <= 100000:
                    return precio_eur
            except ValueError:
                continue

    return None

def extraer_envio(texto):
    texto_lower = texto.lower()
    terminos_gratis = [
        "envío gratis", "envio gratis", "envío gratuito", "envio gratuito",
        "portes gratis", "portes gratuitos", "entrega gratis", "entrega gratuita",
        "sin gastos de envío", "sin gastos de envio",
        "free shipping", "free delivery", "free delivery to spain",
        "kostenlose lieferung", "kostenloser versand", "gratis versand", "versandkostenfrei",
        "livraison gratuite", "frais de livraison gratuits", "frais de port gratuits",
        "spedizione gratuita", "consegna gratuita",
        "gratis verzending", "kosteloze verzending", "gratis levering",
        "darmowa dostawa", "bezpłatna dostawa", "wysyłka gratis", "dostawa gratis",
    ]
    for termino in terminos_gratis:
        if termino in texto_lower:
            return 0.0

    patrones = [
        r"(?:gastos de |coste de )?env[ií]o\s*(?:desde|de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"(?:gastos de |coste de )?env[ií]o\s*(?:desde|de|:)?\s*€\s*(\d+(?:[.,]\d{1,2})?)",
        r"portes\s*(?:desde|de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"(?:shipping|delivery)(?:\s*to\s*spain)?\s*(?:from|:)?\s*€?\s*(\d+(?:[.,]\d{1,2})?)",
        r"(?:versand|lieferung)(?:\s*nach\s*spanien)?\s*(?:ab|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"versandkosten\s*(?:ab|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"(?:frais de |coût de )?livraison(?:\s*vers\s*l'espagne)?\s*(?:dès|de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"frais de port\s*(?:dès|de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"(?:costi di )?spedizione(?:\s*in\s*spagna)?\s*(?:da|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
    ]
    for patron in patrones:
        coincidencia = re.search(patron, texto_lower)
        if coincidencia:
            try:
                return convertir_numero(coincidencia.group(1))
            except ValueError:
                pass
    return None


def extraer_capacidad(texto, unidad):
    texto = texto.lower()
    unidad = unidad.lower()
    patrones = {
        "tb": r"(\d+(?:[.,]\d+)?)\s*tb",
        "gb": r"(\d+(?:[.,]\d+)?)\s*gb",
        "kg": r"(\d+(?:[.,]\d+)?)\s*kg",
        "g": r"(\d+(?:[.,]\d+)?)\s*g\b",
        "l": r"(\d+(?:[.,]\d+)?)\s*l\b",
        "ml": r"(\d+(?:[.,]\d+)?)\s*ml",
    }
    patron = patrones.get(unidad)
    if not patron:
        return None
    coincidencia = re.search(patron, texto)
    if not coincidencia:
        return None
    try:
        return convertir_numero(coincidencia.group(1))
    except ValueError:
        return None


# ============================================================
# PAISES Y TIENDAS DE LA UNION EUROPEA
# ============================================================

PAISES_UE_TLD = {
    "es", "de", "fr", "it", "pt", "nl", "be", "at",
    "pl", "se", "dk", "ie", "fi", "cz", "gr", "eu"
}

DOMINIOS_FUERA_UE = (
    ".co.uk", ".uk", ".ch", ".us", ".ca", ".au", ".cn", ".jp", ".ru",
    "amazon.com", "amazon.co.uk", "amazon.ca", "amazon.com.au",
    "ebay.com", "ebay.co.uk", "idealo.co.uk", "scan.co.uk",
    "ebuyer.com", "overclockers.co.uk", "box.co.uk", "vinted.co.uk"
)

DOMINIOS_IGNORADOS = (
    "bing.com", "google.com", "yahoo.com", "duckduckgo.com", "doubleclick.net",
    "yandex.com", "baidu.com", "msn.com"
)


def canonicalizar_url(url):
    """
    Normaliza URLs para limpiar parámetros de tracking sin romper la ruta original del marketplace.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")

        # Amazon: unificar a https://www.amazon.<tld>/dp/<ASIN>
        if "amazon." in domain:
            m_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', parsed.path)
            if m_asin:
                asin = m_asin.group(1)
                return f"https://www.{domain}/dp/{asin}"

        # Para Wallapop, Vinted y el resto de tiendas, conservar la ruta completa limpia de query params.
        # ¡IMPORTANTE!: Wallapop requiere el slug del producto en la URL para no devolver HTTP 404.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return url


def clave_deduplicacion_url(url):
    """
    Genera una clave única de producto para evitar procesar la misma oferta duplicada
    (ej. el mismo ASIN de Amazon o el mismo ID de anuncio de Wallapop/Vinted/eBay)
    sin modificar la URL original necesaria para visitar la web.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")

        if "amazon." in domain:
            m_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', parsed.path)
            if m_asin:
                return f"amazon:{domain}:{m_asin.group(1)}"

        if "wallapop.com" in domain:
            m_w = re.search(r'/item/(?:.*-)?(\d+)', parsed.path)
            if m_w:
                return f"wallapop:{m_w.group(1)}"

        if "vinted." in domain:
            m_v = re.search(r'/items/(\d+)', parsed.path)
            if m_v:
                return f"vinted:{m_v.group(1)}"

        if "ebay." in domain:
            m_e = re.search(r'/itm/(?:.*-)?(\d+)', parsed.path)
            if m_e:
                return f"ebay:{m_e.group(1)}"

        return canonicalizar_url(url)
    except Exception:
        return url


def es_entorno_datacenter():
    """
    Determina si la ejecución actual es en centro de datos (GitHub Actions / Azure / CI)
    o en entorno residencial / local (NAS o equipo propio con IP doméstica).
    """
    modo_res = os.getenv("MODO_RESIDENCIAL", "").strip().lower()
    if modo_res in ["true", "1", "si", "yes"]:
        return False
    if modo_res in ["false", "0", "no"]:
        return True
    return (os.getenv("GITHUB_ACTIONS") == "true") or (os.getenv("CI") == "true")


def es_tienda_bloqueada_en_datacenter(domain):
    """
    Tiendas con Cloudflare/WAF agresivos que bloquean rangos de IP de centros de datos (Azure, AWS).
    En modo datacenter no se intentan visitar directamente para evitar errores 403 o CAPTCHAs.
    """
    dominios_bloqueados = ("amazon.", "wallapop.", "vinted.")
    return any(d in domain for d in dominios_bloqueados)


def es_tienda_ue(url):
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if any(ign in domain for ign in DOMINIOS_IGNORADOS):
        return False
    if domain.endswith(DOMINIOS_FUERA_UE) or any(d in domain for d in DOMINIOS_FUERA_UE):
        return False
    partes = domain.split(".")
    tld = partes[-1] if partes else ""
    if tld in PAISES_UE_TLD:
        return True
    tiendas_ue_conocidas = [
        # Amazon y marketplaces paneuropeos
        "amazon.es", "amazon.de", "amazon.fr", "amazon.it", "amazon.nl", "amazon.com.be",
        "wallapop", "vinted",
        # Comparadores de precios europeos
        "hoteudeals", "geizhals", "idealo", "guenstiger", "cenypc", "tagdip", "priceinfo",
        # España
        "pccomponentes", "mediamarkt", "coolmod", "neobyte", "aussar", "senetic", "europeanpc",
        "informaticabarata", "shopdutyfree",
        # Alemania
        "alternate", "caseking", "mindfactory", "playox", "office-partner", "galaxus",
        # Bélgica
        "joybuy",
        # Países Bajos / Holanda
        "megekko", "yorcom",
        # Polonia
        "allegro", "proline", "komputronik",
        # Italia
        "soloperaffare",
        # Francia
        "ldlc"
    ]
    if any(t in domain for t in tiendas_ue_conocidas):
        return True
    return False


def detectar_tienda_y_pais(url):
    domain = urlparse(url).netloc.lower().removeprefix("www.")

    # Marketplaces C2C
    if "wallapop" in domain:
        return "Wallapop", "🇪🇸 Wallapop (2ª mano)", False
    if "vinted" in domain:
        return "Vinted", "🇪🇺 Vinted (2ª mano)", True

    # Amazon por país
    if "amazon.es" in domain:
        return "Amazon España", "🇪🇸 España", False
    if "amazon.de" in domain:
        return "Amazon Alemania", "🇩🇪 Alemania", True
    if "amazon.fr" in domain:
        return "Amazon Francia", "🇫🇷 Francia", True
    if "amazon.it" in domain:
        return "Amazon Italia", "🇮🇹 Italia", True
    if "amazon.nl" in domain:
        return "Amazon Países Bajos", "🇳🇱 Países Bajos", True
    if "amazon.com.be" in domain or "amazon.be" in domain:
        return "Amazon Bélgica", "🇧🇪 Bélgica", True

    # Comparadores de precios europeos
    if "hoteudeals" in domain:
        return "HotEUDeals", "🇪🇺 HotEUDeals (Europa)", True
    if "priceinfo" in domain:
        return "PriceInfo", "🇪🇺 PriceInfo (Europa)", True
    if "geizhals" in domain:
        return "Geizhals (Europa)", "🇪🇺 Europa (UE)", True
    if "guenstiger" in domain:
        return "Guenstiger", "🇩🇪 Alemania", True
    if "cenypc" in domain:
        return "CenyPC", "🇵🇱 Polonia", True
    if "tagdip" in domain:
        return "TagDip", "🇵🇱 Polonia", True
    if "idealo" in domain:
        if domain.endswith(".de"): return "Idealo Alemania", "🇩🇪 Alemania", True
        if domain.endswith(".fr"): return "Idealo Francia", "🇫🇷 Francia", True
        if domain.endswith(".it"): return "Idealo Italia", "🇮🇹 Italia", True
        return "Idealo", "🇪🇺 Europa (UE)", True

    # Tiendas Alemania
    if "mindfactory" in domain:
        return "Mindfactory", "🇩🇪 Alemania", True
    if "playox" in domain:
        return "Playox", "🇩🇪 Alemania", True
    if "office-partner" in domain:
        return "Office Partner", "🇩🇪 Alemania", True
    if "galaxus" in domain:
        if domain.endswith(".de"): return "Galaxus Alemania", "🇩🇪 Alemania", True
        return "Galaxus", "🇩🇪 Alemania", True
    if "caseking" in domain:
        return "Caseking", "🇩🇪 Alemania", True
    if "alternate" in domain:
        if domain.endswith(".de"): return "Alternate Alemania", "🇩🇪 Alemania", True
        if domain.endswith(".be"): return "Alternate Bélgica", "🇧🇪 Bélgica", True
        return "Alternate", "🇪🇸 España", False

    # Tiendas Bélgica
    if "joybuy" in domain:
        return "Joybuy", "🇧🇪 Bélgica", True

    # Tiendas Países Bajos / Holanda
    if "megekko" in domain:
        return "Megekko", "🇳🇱 Países Bajos", True
    if "yorcom" in domain:
        return "Yorcom", "🇳🇱 Países Bajos", True

    # Tiendas Polonia
    if "allegro" in domain:
        return "Allegro", "🇵🇱 Polonia", True
    if "proline" in domain:
        return "Proline", "🇵🇱 Polonia", True
    if "komputronik" in domain:
        return "Komputronik", "🇵🇱 Polonia", True

    # Tiendas Italia
    if "soloperaffare" in domain:
        return "SoloPerAffare", "🇮🇹 Italia", True

    # Tiendas Francia
    if "ldlc" in domain:
        return "LDLC", "🇫🇷 Francia", True

    # Tiendas España
    if "mediamarkt" in domain:
        if domain.endswith(".de"): return "MediaMarkt Alemania", "🇩🇪 Alemania", True
        return "MediaMarkt", "🇪🇸 España", False
    if "pccomponentes" in domain:
        if domain.endswith(".pt"): return "PcComponentes Portugal", "🇵🇹 Portugal", True
        return "PcComponentes", "🇪🇸 España", False
    if "informaticabarata" in domain:
        return "Informática Barata", "🇪🇸 España", False
    if "shopdutyfree" in domain:
        return "Shop Duty Free", "🇪🇸 España", False
    if "europeanpc" in domain:
        return "EuropeanPC", "🇪🇸 España", False
    if "senetic" in domain:
        return "Senetic", "🇪🇸 España", False
    if "coolmod" in domain:
        return "Coolmod", "🇪🇸 España", False
    if "neobyte" in domain:
        return "Neobyte", "🇪🇸 España", False
    if "aussar" in domain:
        return "Aussar", "🇪🇸 España", False

    # Fuera de la UE (UK / US)
    if domain.endswith((".co.uk", ".uk")) or "amazon.co.uk" in domain:
        return domain, "🇬🇧 Reino Unido (Fuera UE)", True

    # Fallback por TLD
    if domain.endswith(".es") or any(t in domain for t in ["pccomponentes", "coolmod", "neobyte", "aussar", "senetic", "informaticabarata", "shopdutyfree", "europeanpc"]):
        return domain, "🇪🇸 España", False
    if domain.endswith(".de"): return domain, "🇩🇪 Alemania", True
    if domain.endswith(".pl"): return domain, "🇵🇱 Polonia", True
    if domain.endswith(".nl"): return domain, "🇳🇱 Países Bajos", True
    if domain.endswith(".be"): return domain, "🇧🇪 Bélgica", True
    if domain.endswith(".it"): return domain, "🇮🇹 Italia", True
    if domain.endswith(".fr"): return domain, "🇫🇷 Francia", True
    if domain.endswith(".at"): return domain, "🇦🇹 Austria", True
    if domain.endswith(".pt"): return domain, "🇵🇹 Portugal", True
    if domain.endswith(".eu"): return domain, "🇪🇺 Europa (UE)", True
    return domain, "🇪🇺 Europa (UE)", True


def detectar_condicion(url, titulo="", texto_html=""):
    """
    Detecta si una oferta es Nueva o Reacondicionada / Segunda mano
    (especialmente útil para Amazon Segunda Mano, Amazon Warehouse, Renewed, etc.)
    """
    texto_eval = f"{titulo}".lower()
    url_lower = url.lower()

    # 1. Marketplaces de segunda mano (C2C)
    if "wallapop" in url_lower or "vinted" in url_lower:
        return "Segunda mano (Wallapop/Vinted)"

    # 2. Título o URL explícitos (Amazon, PcComponentes, MediaMarkt, etc.)
    palabras_reacondicionado = [
        "reacondicionado", "reacondicionada", "renewed", "refurbished",
        "segunda mano", "2ª mano", "d'occasion", "gebraucht", "usato",
        "outlet", "como nuevo", "b-ware"
    ]
    if any(p in texto_eval for p in palabras_reacondicionado):
        return "Reacondicionado / Segunda mano"
    if any(p in url_lower for p in ["reacondicionado", "renewed", "warehouse", "outlet"]):
        return "Reacondicionado / Segunda mano"

    # 3. Comprobación en Schema.org JSON-LD
    texto_html_lower = texto_html.lower() if texto_html else ""
    if "schema.org/usedcondition" in texto_html_lower or "schema.org/refurbishedcondition" in texto_html_lower:
        return "Reacondicionado / Segunda mano"

    # 4. Amazon específico: Vendedor o Buy Box de segunda mano
    if "amazon." in url_lower and texto_html:
        # Vendedor de Amazon Warehouse / Segunda mano
        if re.search(r'(?:vendido por|sold by|dispatched from|vendu par|verkauft von)\s*[:]?\s*(?:<[^>]+>)*\s*amazon\s+(?:segunda\s+mano|warehouse|renewed)', texto_html, re.IGNORECASE):
            return "Amazon Segunda Mano (Reacondicionado)"
        
        # Bloque de condición de producto en Amazon
        m_cond = re.search(r'id=[\'"]condition[\'"][^>]*>(.*?)</div>', texto_html, re.IGNORECASE | re.DOTALL)
        if m_cond and any(k in m_cond.group(1).lower() for k in ["segunda mano", "usado", "used", "reacondicionado", "renewed", "d'occasion", "gebraucht"]):
            return "Amazon Segunda Mano (Reacondicionado)"

        # Acordeón de compra usado seleccionado
        if re.search(r'id=[\'"](?:usedBuyBox|usedAccordionRow)[\'"][^>]*class=[\'"][^\'"]*selected', texto_html, re.IGNORECASE):
            return "Amazon Segunda Mano (Reacondicionado)"

    return "Nuevo"


# ============================================================
# VALIDACION Y FILTRADO DE RESULTADOS
# ============================================================

def es_resultado_valido(producto, titulo, descripcion, precio, config, url=""):
    texto = f"{titulo} {descripcion} {url}".lower()

    # 1. Comprobar que pertenezca a la Unión Europea
    if config.get("solo_union_europea", True):
        if not es_tienda_ue(url):
            return False
        # Descartar si el texto contiene precios en libras o dólares
        if "£" in texto or "gbp" in texto or "$" in texto or "usd" in texto:
            return False

    # 2. Comprobar exclusiones
    es_marketplace_c2c = "wallapop" in url.lower() or "vinted" in url.lower()

    if es_marketplace_c2c:
        descartes_c2c = [
            # PALABRAS CLAVE DE VENDIDO/RESERVADO (AÑADIDO)
            "vendido", "vendida", "reservado", "reservada", "no disponible", "ya no está",
            # Inglés
            "sold", "sold out", "reserved", "no longer available",
            # Alemán
            "verkauft", "reserviert", "nicht verfügbar",
            # Francés
            "vendu", "vendue", "réservé", "réservée", "non disponible",
            # Italiano
            "venduto", "venduta", "riservato", "non disponibile",
            # Piezas rotas o accesorios
            "para piezas", "averiado", "averiada", "no funciona",
            "roto", "rota", "defectuoso", "defectuosa", "con fallos",
            "sin probar", "bloqueado", "bloqueada",
            "cable", "carcasa", "caddy", "funda", "dock", "adaptador"
        ]
        for termino in descartes_c2c:
            if termino in texto:
                return False

        # Descartar anuncios con fechas de publicación antiguas (meses o semanas)
        # En Wallapop y Vinted las ofertas buenas vuelan en horas o pocos días.
        if config.get("ignorar_vendidos", True):
            patron_antiguo = re.search(
                r'\b(?:hace\s+\d+\s+(?:mes(?:es)?|año(?:s)?|sem(?:ana)?s?)|'
                r'\d+\s+(?:months?|weeks?|years?)\s+ago|'
                r'il y a\s+\d+\s+(?:mois|semaines?|ans?)|'
                r'vor\s+\d+\s+(?:monaten?|wochen?|jahren?))\b',
                texto,
                re.IGNORECASE
            )
            if patron_antiguo:
                return False
    else:
        exclusiones = (
            producto.get("excluir_terminos")
            if producto.get("excluir_terminos") is not None
            else config.get("excluir_terminos", TERMINOS_EXCLUSION_DEFECTO)
        )
        for termino in exclusiones:
            if termino.lower() in texto:
                return False

    # 3. Comprobar que contenga el modelo o términos clave obligatorios
    texto_titulo_url = f"{titulo} {url}".lower()
    requeridos = producto.get("terminos_requeridos")
    if requeridos:
        for req in requeridos:
            if req.lower() not in texto:
                return False
    else:
        tokens = producto.get("busqueda", "").split()
        for token in tokens:
            token_clean = token.lower().strip()
            if len(token_clean) < 3:
                continue

            # Si el token es un código de modelo técnico (contiene letras y números, ej. WD60EFPX, WD80EFPX)
            # EXIGIMOS que esté presente en el TÍTULO o en la URL del producto.
            # No basta con que aparezca en la descripción o resumen de búsqueda, porque páginas de modelos antiguos (ej. WD60EFRX)
            # suelen mencionar modelos modernos en carruseles de sugerencias o comparativas.
            es_codigo_modelo = bool(re.search(r'[a-z]', token_clean) and re.search(r'\d', token_clean) and len(token_clean) >= 4)
            if es_codigo_modelo:
                modelo_base = token_clean.split("-")[0]
                if token_clean not in texto_titulo_url and modelo_base not in texto_titulo_url:
                    return False
            else:
                if token_clean not in texto:
                    return False

    # 4. Comprobar concordancia de capacidad (ej. evitar alertar de 4TB si se busca 8TB)
    capacidad = producto.get("capacidad")
    unidad = producto.get("unidad_medida")
    if capacidad and unidad:
        cap_str = f"{capacidad}{unidad}".lower()
        cap_str_space = f"{capacidad} {unidad}".lower()
        cap_str_dash = f"{capacidad}-{unidad}".lower()
        
        cap_fr = f"{capacidad}to" if str(unidad).upper() == "TB" else (f"{capacidad}go" if str(unidad).upper() == "GB" else "")
        cap_fr_space = f"{capacidad} to" if str(unidad).upper() == "TB" else (f"{capacidad} go" if str(unidad).upper() == "GB" else "")
        cap_fr_dash = f"{capacidad}-to" if str(unidad).upper() == "TB" else (f"{capacidad}-go" if str(unidad).upper() == "GB" else "")

        busqueda_modelo = str(producto.get("busqueda", "")).lower()
        modelo_en_texto = bool(busqueda_modelo and len(busqueda_modelo) >= 5 and busqueda_modelo in texto)

        coincide_cap = (
            cap_str in texto or cap_str_space in texto or cap_str_dash in texto
            or (cap_fr and (cap_fr in texto or cap_fr_space in texto or cap_fr_dash in texto))
            or modelo_en_texto
        )
        if not coincide_cap:
            return False

        # Si el título menciona explícitamente otra capacidad distinta y NO la buscada, descartar
        if str(unidad).upper() == "TB":
            otras_capacidades = [c for c in [1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24] if c != int(capacidad)]
            titulo_lower = titulo.lower()
            tiene_otra_en_titulo = any(
                f"{c}tb" in titulo_lower or f"{c} tb" in titulo_lower or f"{c}-tb" in titulo_lower or
                f"{c}to" in titulo_lower or f"{c} to" in titulo_lower or f"{c}-to" in titulo_lower
                for c in otras_capacidades
            )
            tiene_buscada_en_titulo = (
                cap_str in titulo_lower or cap_str_space in titulo_lower or cap_str_dash in titulo_lower or
                (cap_fr and (cap_fr in titulo_lower or cap_fr_space in titulo_lower or cap_fr_dash in titulo_lower)) or
                (busqueda_modelo and busqueda_modelo in titulo_lower)
            )
            if tiene_otra_en_titulo and not tiene_buscada_en_titulo:
                return False

    # 5. Comprobar precio mínimo razonable
    precio_min = producto.get("precio_minimo_unidad")
    if precio_min is None:
        max_u = producto.get("precio_maximo_unidad")
        if max_u:
            precio_min = float(max_u) * 0.25
    if precio_min is not None and precio < float(precio_min):
        return False

    return True


# ============================================================
# CALCULO DE COSTES Y EVALUACION DE CRITERIOS
# ============================================================

def calcular_costes(producto, precio, envio, config, es_internacional=False, url=""):
    cantidad = int(producto.get("cantidad", 1))
    incluir_envio = config.get("incluir_envio", True)
    envio_por_defecto = config.get("envio_por_defecto", 4.50)
    envio_europa_estimado = config.get("envio_europa_estimado", 6.50)
    envio_c2c_estimado = config.get("envio_wallapop_vinted_estimado", 4.50)

    es_c2c = "wallapop" in url.lower() or "vinted" in url.lower()
    envio_conocido = envio is not None
    coste_envio = 0.0
    origen_envio = "Confirmado"

    if incluir_envio:
        if envio_conocido:
            coste_envio = float(envio)
            origen_envio = "GRATIS (0.00 €)" if coste_envio == 0.0 else f"{coste_envio:.2f} € (Confirmado en tienda)"
        elif es_c2c:
            coste_envio = float(envio_c2c_estimado)
            origen_envio = f"{coste_envio:.2f} € (Envío + Protección al comprador estimado)"
        elif es_internacional:
            coste_envio = float(envio_europa_estimado)
            origen_envio = f"{coste_envio:.2f} € (Estimado envío internacional a España)"
        elif envio_por_defecto is not None:
            coste_envio = float(envio_por_defecto)
            origen_envio = f"{coste_envio:.2f} € (Estimado nacional)"
        else:
            coste_envio = 0.0
            origen_envio = "Desconocido"

    if es_c2c:
        total_pedido = precio + coste_envio
        total_unidad = total_pedido
    else:
        total_pedido = (precio * cantidad) + coste_envio
        total_unidad = total_pedido / cantidad

    return {
        "precio_base": precio,
        "envio": envio,
        "envio_conocido": envio_conocido,
        "es_internacional": es_internacional,
        "es_c2c": es_c2c,
        "coste_envio_aplicado": coste_envio,
        "origen_envio": origen_envio,
        "cantidad": 1 if es_c2c else cantidad,
        "total_unidad": total_unidad,
        "total_pedido": total_pedido,
    }


def calcular_precio_medida(producto, total_unidad):
    limite = producto.get("precio_maximo_por_unidad_medida")
    if limite is None:
        return None
    medida = producto.get("unidad_medida")
    if not medida:
        return None
    capacidad = producto.get("capacidad") or producto.get("capacidad_por_unidad")
    if capacidad is None:
        capacidad = extraer_capacidad(
            f"{producto.get('busqueda', '')} {producto.get('nombre', '')}",
            medida
        )
    if not capacidad or capacidad <= 0:
        return None
    return total_unidad / float(capacidad)


def evaluar_oferta(producto, costes, precio_medida, config):
    incluir_envio = config.get("incluir_envio", True)
    exigir_confirmado = config.get("exigir_envio_confirmado", False)

    if incluir_envio and exigir_confirmado and not costes["envio_conocido"]:
        return {
            "cumple": False,
            "motivo_descarte": "Gastos de envío no confirmados en el texto (se exige confirmación explícita)",
            "precio_medida": precio_medida,
            **costes,
        }

    criterios_definidos = 0
    criterios_cumplidos = 0

    max_unidad = producto.get("precio_maximo_unidad")
    if max_unidad is not None:
        criterios_definidos += 1
        if costes["total_unidad"] <= float(max_unidad):
            criterios_cumplidos += 1

    max_pedido = producto.get("precio_maximo_pedido")
    if max_pedido is not None:
        criterios_definidos += 1
        if costes["total_pedido"] <= float(max_pedido):
            criterios_cumplidos += 1

    max_medida = producto.get("precio_maximo_por_unidad_medida")
    if max_medida is not None:
        criterios_definidos += 1
        if precio_medida is not None and precio_medida <= float(max_medida):
            criterios_cumplidos += 1

    cumple = (criterios_definidos > 0) and (criterios_cumplidos == criterios_definidos)

    return {
        "cumple": cumple,
        "motivo_descarte": None if cumple else "No cumple los límites de precio definidos",
        "precio_medida": precio_medida,
        **costes,
    }


# ============================================================
# HISTORICO CSV
# ============================================================

def guardar_historico(producto, oferta, evaluacion):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    existe = HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_CSV)
        if not existe or HISTORY_FILE.stat().st_size == 0:
            escritor.writeheader()
        escritor.writerow({
            "fecha": datetime.now(timezone.utc).isoformat(),
            "producto_id": producto["id"],
            "producto": producto["nombre"],
            "tienda": oferta["tienda_nombre"],
            "pais": oferta["pais"],
            "precio_producto": f"{oferta['precio']:.2f}",
            "envio": evaluacion["origen_envio"],
            "envio_confirmado": "SI" if evaluacion["envio_conocido"] else "NO",
            "total_unidad_con_envio": f"{evaluacion['total_unidad']:.2f}",
            "cantidad": evaluacion["cantidad"],
            "total_pedido_con_envio": f"{evaluacion['total_pedido']:.2f}",
            "precio_unidad_medida": (
                f"{evaluacion['precio_medida']:.2f}"
                if evaluacion["precio_medida"] is not None
                else ""
            ),
            "cumple_objetivo": "SI" if evaluacion["cumple"] else "NO",
            "url": oferta["url"],
        })


# ============================================================
# TELEGRAM
# ============================================================

def obtener_credenciales_telegram(config=None):
    # 1. Variables de entorno (Secrets de GitHub o Docker .env)
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("BOT_TOKEN")
        or os.getenv("bot_token")
        or os.getenv("telegram_bot_token")
    )
    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID")
        or os.getenv("CHAT_ID")
        or os.getenv("chat_id")
        or os.getenv("telegram_chat_id")
    )

    # 2. Si no están en variables de entorno, buscar en config.json (ideal para modo residencial / NAS)
    if not token or not chat_id:
        if config is None:
            try:
                config = cargar_configuracion()
            except Exception:
                config = {}
        if config and isinstance(config, dict):
            if not token:
                token = (
                    config.get("telegram_bot_token")
                    or config.get("bot_token")
                    or config.get("TELEGRAM_BOT_TOKEN")
                    or config.get("BOT_TOKEN")
                )
            if not chat_id:
                chat_id = (
                    config.get("telegram_chat_id")
                    or config.get("chat_id")
                    or config.get("TELEGRAM_CHAT_ID")
                    or config.get("CHAT_ID")
                )

    return (str(token).strip() if token else None), (str(chat_id).strip() if chat_id else None)


def enviar_telegram(mensaje, config=None):
    token, chat_id = obtener_credenciales_telegram(config)
    if not token or not chat_id:
        faltantes = []
        if not token: faltantes.append("TELEGRAM_BOT_TOKEN (en variables de entorno o en config.json)")
        if not chat_id: faltantes.append("TELEGRAM_CHAT_ID (en variables de entorno o en config.json)")
        print(f"  ⚠️ No se puede enviar alerta. Falta: {', '.join(faltantes)}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": mensaje,
        "link_preview_options": {"is_disabled": False},
    }
    try:
        respuesta = requests.post(url, json=payload, timeout=20)
        respuesta.raise_for_status()
        datos = respuesta.json()
        if not datos.get("ok"):
            print(f"  ❌ Telegram API devolvió error: {datos}")
            return False
        return True
    except requests.RequestException as error:
        print(f"  ❌ Error enviando mensaje a Telegram: {error}")
        return False


def crear_mensaje(producto, oferta, evaluacion, bajada=False):
    moneda = "€"
    titulo = "📉 BAJADA DE PRECIO" if bajada else "🛒 NUEVA OFERTA"
    tienda = oferta.get("tienda_nombre", oferta["tienda"])
    pais = oferta.get("pais", "🇪🇺 Europa")
    condicion = oferta.get("condicion", "Nuevo")

    if "reacondicionado" in condicion.lower() or "segunda mano" in condicion.lower() or "renewed" in condicion.lower():
        estado_str = f"♻️ {condicion}"
    else:
        estado_str = "🆕 Nuevo"

    mensaje = (
        f"{titulo}\n\n"
        f"📦 {producto['nombre']}\n"
        f"🏪 Tienda: {tienda} ({pais})\n"
        f"🏷️ Estado: {estado_str}\n\n"
        f"💶 Precio producto: {oferta['precio']:.2f} {moneda}\n"
        f"🚚 Gastos de envío a España: {evaluacion['origen_envio']}\n"
        f"💰 Total unidad (con envío): {evaluacion['total_unidad']:.2f} {moneda}\n"
        f"🔢 Cantidad pedido: {evaluacion['cantidad']}\n"
        f"🧾 Total pedido (con envío): {evaluacion['total_pedido']:.2f} {moneda}\n"
    )
    if evaluacion["precio_medida"] is not None:
        unidad = producto.get("unidad_medida", "")
        mensaje += f"📊 Coste por {unidad}: {evaluacion['precio_medida']:.2f} {moneda}/{unidad}\n"
    if evaluacion["es_internacional"]:
        mensaje += "\nℹ️ Tienda europea con envío a España. Verifica el importe final en el carrito.\n"
    elif not evaluacion["envio_conocido"]:
        mensaje += "\n⚠️ Gastos de envío estimados. Verifica antes de comprar.\n"
    mensaje += f"\n🔗 ENLACE A LA OFERTA:\n{oferta['url']}"
    return mensaje


# ============================================================
# PROCESAMIENTO Y CONTROL DE OFERTAS
# ============================================================

def clave_oferta(producto, oferta):
    base = f"{producto['id']}|{oferta['url']}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def procesar_oferta(producto, oferta, config, estado):
    costes = calcular_costes(
        producto, oferta["precio"], oferta["envio"], config,
        es_internacional=oferta["es_internacional"], url=oferta["url"]
    )
    precio_medida = calcular_precio_medida(producto, costes["total_unidad"])
    evaluacion = evaluar_oferta(producto, costes, precio_medida, config)

    guardar_historico(producto, oferta, evaluacion)

    clave = clave_oferta(producto, oferta)
    ofertas_guardadas = estado.setdefault("ofertas", {})
    anterior = ofertas_guardadas.get(clave)

    precio_actual = evaluacion["total_unidad"]
    es_nueva = anterior is None
    ha_bajado = (
        anterior is not None
        and anterior.get("precio") is not None
        and precio_actual < float(anterior["precio"])
    )

    avisar = False
    es_bajada = False

    if evaluacion["cumple"]:
        if es_nueva:
            avisar = True
        elif ha_bajado and config.get("notificar_bajadas", True):
            avisar = True
            es_bajada = True
        elif not config.get("solo_nuevas_ofertas", True) and not ha_bajado:
            avisar = True

    if avisar:
        print(f"  🔔 ¡Oferta cumple criterios! Enviando a Telegram: {evaluacion['total_unidad']:.2f} €/unidad ({oferta['tienda_nombre']})...")
        mensaje = crear_mensaje(producto, oferta, evaluacion, es_bajada)
        if enviar_telegram(mensaje, config=config):
            print("  ✅ Notificación de Telegram enviada con éxito.")
            ofertas_guardadas[clave] = {
                "precio": precio_actual,
                "ultima_alerta": datetime.now(timezone.utc).isoformat(),
            }
        else:
            print("  ❌ No se pudo enviar la notificación a Telegram.")
    else:
        if evaluacion.get("motivo_descarte"):
            print(f"  ℹ️ [{oferta['pais']}] {oferta['tienda_nombre']} ({oferta['precio']:.2f} €): {evaluacion['motivo_descarte']}")
        if anterior is None:
            ofertas_guardadas[clave] = {"precio": precio_actual, "ultima_alerta": None}
        elif ha_bajado:
            ofertas_guardadas[clave]["precio"] = precio_actual


# ============================================================
# GENERACION DE CONSULTAS Y BUSQUEDA RESILIENTE (EUROPA)
# ============================================================

def generar_consultas(producto, config):
    busqueda = producto.get("busqueda", "").strip()
    buscar_europa = config.get("solo_union_europea", True)
    consultas = []

    tokens = busqueda.split()
    modelos = [t for t in tokens if re.search(r"[A-Za-z]", t) and re.search(r"\d", t) and len(t) >= 5]

    if modelos:
        termino = modelos[0].split("-")[0]
        termino_c2c = modelos[0]
    else:
        termino = busqueda
        termino_c2c = busqueda

    # 1. COMPARADORES EUROPEOS Y AMAZON UE (Donde se agregan ofertas de toda la UE)
    if buscar_europa:
        consultas.append((
            f'"{termino}" (site:hoteudeals.com OR site:geizhals.de OR site:idealo.de OR site:idealo.es OR site:guenstiger.de OR site:priceinfo.eu OR site:cenypc.pl OR site:amazon.de OR site:amazon.it)',
            "es-es"
        ))

        # 2. GRANDES TIENDAS EUROPA (Alemania, Países Bajos, Bélgica, Polonia, Italia)
        consultas.append((
            f'"{termino}" (site:mindfactory.de OR site:playox.de OR site:galaxus.de OR site:megekko.nl OR site:yorcom.nl OR site:allegro.pl OR site:proline.pl OR site:komputronik.pl OR site:soloperaffare.it)',
            "es-es"
        ))

        # 3. TIENDAS ESPAÑA
        consultas.append((
            f'"{termino}" (site:amazon.es OR site:pccomponentes.com OR site:mediamarkt.es OR site:informaticabarata.com OR site:shopdutyfree.es OR site:senetic.es OR site:europeanpc.es)',
            "es-es"
        ))
    
    # 4. MARKETPLACES C2C (Wallapop / Vinted): Exigimos divisa y excluimos vendidos
    if config.get("incluir_wallapop_vinted", True):
        consultas.append((
            f'"{termino_c2c}" (site:wallapop.com OR site:vinted.es) -vendido -reservado ("€" OR "EUR")', 
            "es-es"
        ))
    
    # 5. BÚSQUEDA GENERAL DE PRECIO (Europa)
    consultas.append((f'"{termino}" (precio OR offer OR price OR preis) ("€" OR "EUR" OR "zł")', "es-es"))

    # Eliminar duplicados
    vistos = set()
    unicas = []
    for c, reg in consultas:
        if c not in vistos:
            vistos.add(c)
            unicas.append((c, reg))
    return unicas

def buscar_en_ddgs(buscador, consulta, region):
    # En GitHub Actions, DuckDuckGo limita rápidamente si se consulta muy rápido.
    # Probamos auto, html y lite con reintento si hay rate limit.
    for backend in ["auto", "html", "lite"]:
        try:
            res = buscador.text(consulta, region=region, backend=backend, max_results=10)
            if res:
                return list(res)
        except Exception as e:
            err = str(e).lower()
            if "429" in err or "ratelimit" in err or "202" in err:
                print(f"   ⏳ Rate limit de DuckDuckGo en '{backend}'. Pausando 3s antes del siguiente intento...")
                time.sleep(3.0)
            continue
    return []


def consultar_url_directa(url, producto, config):
    try:
        cookies = {}
        if "amazon." in url.lower():
            cookies = {"lc-main": "es_ES", "i18n-prefs": "EUR"}

        # Si curl_cffi está disponible, imita la huella TLS de Chrome
        if curl_requests:
            try:
                respuesta = curl_requests.get(
                    url,
                    impersonate="chrome124",
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Referer": "https://www.google.com/",
                    },
                    cookies=cookies if cookies else None,
                    allow_redirects=True,
                    timeout=(8.0, 15.0)
                )
            except Exception:
                respuesta = None
        else:
            respuesta = None

        # Fallback con requests estándar
        if respuesta is None:
            session = get_robust_session()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": "https://www.google.com/",
            }
            if "amazon." in url:
                domain = urlparse(url).netloc.lower()
                if not domain.startswith("."):
                    domain = "." + domain
                session.cookies.set('lc-main', 'es_ES', domain=domain)
                session.cookies.set('i18n-prefs', 'EUR', domain=domain)
            respuesta = session.get(url, headers=headers, allow_redirects=True, timeout=(8.0, 15.0))
        
        if respuesta.status_code != 200:
            if respuesta.status_code in [403, 503]:
                print(f"   🚫 Bloqueado ({respuesta.status_code}) por el sitio web: {urlparse(url).netloc}")
            else:
                print(f"   🚫 Error HTTP {respuesta.status_code} en {urlparse(url).netloc}")
            return None
            
        texto = respuesta.text
        texto_lower = texto.lower()
        
        # 1. Detección de CAPTCHA / Robot Check
        if "validatecaptcha" in texto_lower or "enter the characters" in texto_lower or "robot check" in texto_lower or ("algo ha salido mal" in texto_lower and "captcha" in texto_lower):
            print(f"   🚨 Bloqueo con CAPTCHA detectado en {urlparse(url).netloc} (habitual en visitas directas a Amazon desde servidores)")
            return None

        # 2. Extraer título real de la página
        titulo_real = producto["nombre"]
        m_title = re.search(r'<title[^>]*>(.*?)</title>', texto, re.IGNORECASE | re.DOTALL)
        if m_title:
            t = html.unescape(m_title.group(1)).strip()
            t = re.sub(r'\s+', ' ', t)
            if len(t) > 3:
                titulo_real = t

        # 3. Filtro de disponibilidad para Marketplaces C2C (Wallapop, Vinted, eBay)
        precio = None
        if any(c in url.lower() for c in ["wallapop", "vinted", "ebay"]):
            # A) Comprobar si el título ya indica vendido o reservado
            if any(p in titulo_real.lower() for p in ["vendido", "reservado", "sold", "vendu", "verkauft", "no disponible"]):
                print(f"   ⏭️ Descartado (Vendido/Reservado en título): {url}")
                return None

            # B) WALLAPOP: Comprobación exhaustiva en JSON interno (__NEXT_DATA__)
            if "wallapop" in url.lower():
                m_next = re.search(r"""<script[^>]*id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""", texto, re.DOTALL | re.IGNORECASE)
                if m_next:
                    try:
                        data = json.loads(m_next.group(1))
                        page_props = data.get("props", {}).get("pageProps", {})
                        if page_props.get("statusCode") in [404, 410] or page_props.get("error"):
                            print(f"   ⏭️ Descartado (Página no encontrada / 404 en Wallapop): {url}")
                            return None
                        
                        item_data = page_props.get("item")
                        if not item_data and "item" in page_props:
                            print(f"   ⏭️ Descartado (Artículo eliminado en Wallapop): {url}")
                            return None
                            
                        if isinstance(item_data, dict):
                            flags = item_data.get("flags", {})
                            if flags.get("sold") or flags.get("reserved") or flags.get("expired") or flags.get("onHold"):
                                motivo = "Vendido" if flags.get("sold") else ("Reservado" if flags.get("reserved") else "No disponible/Pausado")
                                print(f"   ⏭️ Descartado ({motivo} en Wallapop): {url}")
                                return None
                            
                            status = str(item_data.get("status", "")).lower()
                            if status in ["sold", "reserved", "vendido", "reservado"]:
                                print(f"   ⏭️ Descartado (Estado '{status}' en Wallapop): {url}")
                                return None

                            # Obtener precio exacto desde el objeto de Wallapop
                            price_cash = item_data.get("price", {}).get("cash", {}).get("amount")
                            if price_cash is not None:
                                precio = float(price_cash)
                                
                            t_orig = item_data.get("title")
                            if isinstance(t_orig, dict):
                                t_orig = t_orig.get("original")
                            if t_orig and len(t_orig) > 3:
                                titulo_real = t_orig
                    except Exception:
                        pass

            # C) VINTED: Comprobación de estado y marcadores
            if "vinted" in url.lower():
                m_next = re.search(r"""<script[^>]*id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""", texto, re.DOTALL | re.IGNORECASE)
                if m_next:
                    try:
                        data = json.loads(m_next.group(1))
                        item_data = data.get("props", {}).get("pageProps", {}).get("item", {})
                        if item_data.get("status") in ["sold", "reserved", "closed"] or item_data.get("is_closed"):
                            print(f"   ⏭️ Descartado (Vendido o cerrado en Vinted): {url}")
                            return None
                    except Exception:
                        pass
                
                marcadores_vinted = [
                    "cet article a été vendu", "artículo vendido", "dieser artikel wurde verkauft",
                    "item sold", "article vendu", "venduto", "verkauft"
                ]
                if any(m in texto_lower for m in marcadores_vinted):
                    print(f"   ⏭️ Descartado (Marcador de vendido en Vinted): {url}")
                    return None

            # D) EBAY: Comprobación de anuncios finalizados o sin existencias
            if "ebay." in url.lower():
                marcadores_ebay = [
                    "este anuncio ha finalizado", "anuncio finalizado", "this listing was ended",
                    "this listing has ended", "el vendedor ha finalizado este anuncio",
                    "este artículo no está disponible", "0 disponibles", "0 available",
                    "dieses angebot wurde beendet", "cette annonce est terminée"
                ]
                if any(m in texto_lower for m in marcadores_ebay):
                    print(f"   ⏭️ Descartado (Anuncio finalizado o agotado en eBay): {url}")
                    return None

            # E) Marcadores genéricos de no disponible en HTML
            frases_no_disponible = [
                "este producto ya se ha vendido", "producto vendido", "artículo vendido", "¡vendido!",
                "ya no está disponible", "artículo no disponible", "cet article a été vendu",
                "article vendu", "dieser artikel wurde verkauft", "item sold", "no longer available"
            ]
            if any(f in texto_lower for f in frases_no_disponible):
                print(f"   ⏭️ Descartado (Marcador de no disponible en página): {url}")
                return None

            if re.search(r'(?:estado|statut|status).*?:.*?(?:vendido|sold|verkauft|vendu|reservado|réservé)', texto, re.IGNORECASE):
                print(f"   ⏭️ Descartado (Estado marcado como vendido/reservado): {url}")
                return None

        # 4. Intentar extraer precio
        precio = extraer_precio(texto)
        if not precio:
            # Solo si NO hay precio comprobamos si está agotado en Amazon
            if "amazon." in url.lower() and re.search(r'(?:currently unavailable|temporarily out of stock|no disponible|agotado|sin stock)', texto, re.IGNORECASE):
                print(f"   ⏭️ Descartado: Producto sin precio / agotado en Amazon: {urlparse(url).netloc}")
            else:
                print(f"   ⚠️ No se pudo extraer el precio del HTML en {urlparse(url).netloc}")
            return None
            
        tienda_nombre, pais, es_inter = detectar_tienda_y_pais(url)
        envio = extraer_envio(texto)
        condicion = detectar_condicion(url, titulo_real, texto)
        
        if envio is None and "Amazon España" in tienda_nombre and precio >= 35.0:
            envio = 0.0
            
        return {
            "producto_id": producto["id"],
            "producto": producto["nombre"],
            "tienda": urlparse(url).netloc.lower().removeprefix("www."),
            "tienda_nombre": tienda_nombre,
            "pais": pais,
            "es_internacional": es_inter,
            "precio": precio,
            "envio": envio,
            "url": url,
            "titulo": titulo_real,
            "condicion": condicion,
        }
        
    except Exception as e:
        print(f"   ⚠️ Error inesperado en {url}: {e}")
        return None
                                
def buscar_producto(producto, pais_default, config):
    consultas = generar_consultas(producto, config)
    resultados = []
    vistos = set()

    datacenter = es_entorno_datacenter()

    # 1. URLs fijas configuradas en el JSON
    urls_fijas = []
    if producto.get("url"):
        urls_fijas.append(producto["url"])
    if producto.get("urls") and isinstance(producto["urls"], list):
        urls_fijas.extend(producto["urls"])

    for u in urls_fijas:
        canon_u = canonicalizar_url(u)
        u_domain = urlparse(canon_u).netloc.lower().removeprefix("www.")
        u_dedup = clave_deduplicacion_url(canon_u)
        if any(ign in u_domain for ign in DOMINIOS_IGNORADOS) or canon_u in vistos or u in vistos or u_dedup in vistos:
            continue

        if datacenter and es_tienda_bloqueada_en_datacenter(u_domain):
            print(f"   ℹ️ [Nube] Omitiendo visita directa a URL fija de {u_domain} para evitar bloqueo anti-bot")
            continue

        directa = consultar_url_directa(canon_u, producto, config)
        if directa:
            if es_resultado_valido(producto, directa["titulo"], "", directa["precio"], config, directa["url"]):
                vistos.add(canon_u)
                vistos.add(u)
                vistos.add(u_dedup)
                vistos.add(directa["url"])
                resultados.append(directa)
            else:
                print(f"   ⏭️ Descartado por filtros/precio: {directa['precio']:.2f} € en {directa['tienda']}")
        time.sleep(random.uniform(0.5, 1.5))

    # 2. Búsqueda con DuckDuckGo (para PcComponentes, Wallapop, Vinted, Idealo, MediaMarkt, etc.)
    try:
        with DDGS() as buscador:
            for i, (consulta, region) in enumerate(consultas):
                if i > 0:
                    time.sleep(random.uniform(0.6, 1.2))
                encontrados = buscar_en_ddgs(buscador, consulta, region)
                for res in encontrados:
                    raw_url = res.get("href", "")
                    if not raw_url.startswith(("http://", "https://")):
                        continue

                    url = canonicalizar_url(raw_url)
                    domain = urlparse(url).netloc.lower().removeprefix("www.")
                    dedup_key = clave_deduplicacion_url(url)

                    # Descartar buscadores, redes publicitarias (ej. bing.com/aclick) y URLs ya vistas
                    if (
                        any(ign in domain for ign in DOMINIOS_IGNORADOS)
                        or url in vistos
                        or raw_url in vistos
                        or dedup_key in vistos
                    ):
                        continue

                    # 🛡️ FILTRO ESTRICTO DE PÁGINAS DE PRODUCTO VS BÚSQUEDAS/CATEGORÍAS:
                    # A) En Amazon, SOLO permitir URLs de producto individual (/dp/ o /gp/product/).
                    #    Ignorar páginas de búsqueda (/s, /s?, /brand/s), listas y categorías que provocan CAPTCHAs.
                    if "amazon." in domain:
                        if not ("/dp/" in url.lower() or "/gp/product/" in url.lower()):
                            continue

                    # B) En Wallapop, SOLO permitir artículos individuales (/item/)
                    if "wallapop.com" in domain:
                        if "/item/" not in url.lower():
                            continue

                    # C) En Vinted, SOLO permitir artículos individuales (/items/)
                    if "vinted." in domain:
                        if "/items/" not in url.lower():
                            continue

                    # D) En eBay, SOLO permitir artículos individuales (/itm/)
                    if "ebay." in domain:
                        if "/itm/" not in url.lower():
                            continue

                    titulo = res.get("title", "")
                    descripcion = res.get("body", "")
                    texto = f"{titulo} {descripcion}"

                    # COMPROBAR SI ES UNA TIENDA QUE BLOQUEA SERVIDORES
                    es_bloqueada = datacenter and es_tienda_bloqueada_en_datacenter(domain)

                    if not es_bloqueada:
                        # TIENDA ACCESIBLE / VERIFICABLE (MediaMarkt, PcComponentes, Idealo, Geizhals, etc.)
                        # NUNCA nos fiamos ciegamente de un fragmento de búsqueda: visitamos la URL directa para verificar precio real
                        termino_busqueda = producto.get("busqueda", "").lower()
                        palabras_clave = [p for p in termino_busqueda.split() if len(p) >= 3][:2]
                        if not palabras_clave:
                            palabras_clave = [p for p in producto.get("nombre", "").lower().split() if len(p) >= 3][:2]
                        
                        texto_para_relevancia = f"{titulo} {url}".lower()
                        es_relevante = any(p in texto_para_relevancia for p in palabras_clave)

                        if es_relevante:
                            print(f"   🔍 Verificando precio en directo en {domain}: {url}")
                            directa = consultar_url_directa(url, producto, config)
                            if directa:
                                if es_resultado_valido(producto, directa["titulo"], "", directa["precio"], config, directa["url"]):
                                    resultados.append(directa)
                                    vistos.add(url)
                                    vistos.add(raw_url)
                                    vistos.add(dedup_key)
                                    vistos.add(directa["url"])
                                else:
                                    print(f"   ⏭️ Descartado por filtros/precio: {directa['precio']:.2f} € en {directa['tienda']}")
                                    vistos.add(url)
                                    vistos.add(raw_url)
                                    vistos.add(dedup_key)
                            else:
                                # Si la visita directa falla o no tiene precio, no nos fiamos del snippet
                                vistos.add(url)
                                vistos.add(raw_url)
                                vistos.add(dedup_key)
                        continue

                    # COMPROBAR SI ES UN MARKETPLACE DE SEGUNDA MANO (C2C: Wallapop, Vinted, eBay, Milanuncios)
                    es_c2c = any(c in domain for c in ["wallapop", "vinted", "ebay", "milanuncios"])

                    if es_c2c:
                        # EN ANUNCIOS DE SEGUNDA MANO NUNCA NOS FIAMOS DEL SNIPPET DE DDG:
                        # Los anuncios se venden en horas o se borran (HTTP 404), mientras los buscadores
                        # mantienen la información vieja en caché durante semanas.
                        # Exigimos SIEMPRE confirmación en directo:
                        print(f"   🔍 Verificando disponibilidad en directo de anuncio C2C en {domain}: {url}")
                        directa = consultar_url_directa(url, producto, config)
                        if directa:
                            if es_resultado_valido(producto, directa["titulo"], "", directa["precio"], config, directa["url"]):
                                resultados.append(directa)
                                vistos.add(url)
                                vistos.add(raw_url)
                                vistos.add(dedup_key)
                                vistos.add(directa["url"])
                            else:
                                print(f"   ⏭️ Descartado por filtros/precio: {directa['precio']:.2f} € en {directa['tienda']}")
                                vistos.add(url)
                                vistos.add(raw_url)
                                vistos.add(dedup_key)
                        else:
                            # Si da 404, vendido, reservado o no se puede verificar: descartar sin enviar falsa alarma
                            vistos.add(url)
                            vistos.add(raw_url)
                            vistos.add(dedup_key)
                        continue

                    # TIENDA RETAIL BLOQUEADA EN CENTRO DE DATOS (ej. Amazon en GitHub Actions)
                    # En tiendas oficiales/retail donde los productos no son de segunda mano únicos,
                    # se extrae el precio del snippet de DDG como recurso de contingencia.
                    precio = extraer_precio(texto)
                    if precio is None:
                        continue

                    if not es_resultado_valido(producto, titulo, descripcion, precio, config, url):
                        continue

                    vistos.add(url)
                    vistos.add(raw_url)
                    vistos.add(dedup_key)
                    tienda_nombre, pais, es_inter = detectar_tienda_y_pais(url)
                    envio = extraer_envio(texto)
                    condicion = detectar_condicion(url, titulo, descripcion)

                    if envio is None and "amazon.es" in domain and precio >= 35.0:
                        envio = 0.0

                    resultados.append({
                        "producto_id": producto["id"],
                        "producto": producto["nombre"],
                        "tienda": domain,
                        "tienda_nombre": tienda_nombre,
                        "pais": pais,
                        "es_internacional": es_inter,
                        "precio": precio,
                        "envio": envio,
                        "url": url,
                        "titulo": titulo,
                        "condicion": condicion,
                    })
                
                # Pausa humana entre cada consulta de búsqueda (evita rate limits de DDG)
                time.sleep(random.uniform(2.0, 3.5))
                
    except Exception as error:
        print(f"Error general en buscador para '{producto['nombre']}': {error}")

    return resultados

# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print("=" * 60)
    print("MONITOR UNIVERSAL DE PRECIOS (ESPAÑA Y EUROPA)")
    print("=" * 60)

    config = cargar_configuracion()
    estado = cargar_estado()

    # SE HA ELIMINADO LA COMPROBACIÓN DE 'intervalo_minutos'
    # El script se ejecutará siempre que el workflow lo invoque.

    productos_activos = [p for p in config.get("productos", []) if p.get("activo", True)]
    if not productos_activos:
        print("No hay productos activos configurados.")
        return

    buscar_eu = config.get("solo_union_europea", True)
    modo_busqueda = "Solo Unión Europea (excluye UK)" if buscar_eu else "Solo España"
    print(f"Ámbito de rastreo: {modo_busqueda}")
    print(f"Productos activos a monitorizar: {len(productos_activos)}")

    datacenter = es_entorno_datacenter()
    tipo_red = "Centro de datos (GitHub Actions - Modo protegido)" if datacenter else "Residencial / Local (NAS - Acceso directo completo)"
    print(f"Entorno de red: {tipo_red}")
    print(f"Ruta config: {CONFIG_FILE}")

    token, chat_id = obtener_credenciales_telegram(config)
    if token:
        preview = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else "***"
        print(f"🤖 Bot de Telegram: Conectado (Token: {preview})")
    else:
        print("⚠️ Bot de Telegram: Falta Token (en variables de entorno o en config.json)")

    if chat_id:
        print(f"💬 Chat de Telegram: Conectado (ID: {chat_id})")
    else:
        print("⚠️ Chat de Telegram: Falta Chat ID (en variables de entorno o en config.json)")

    for producto in productos_activos:
        try:
            print(f"\n🔎 Buscando: {producto['nombre']} (Modelo: {producto.get('busqueda')})")
            ofertas = buscar_producto(producto, config.get("pais", "España"), config)
            print(f"   Resultados válidos encontrados: {len(ofertas)}")

            for oferta in ofertas:
                try:
                    procesar_oferta(producto, oferta, config, estado)
                except Exception as err_oferta:
                    print(f"   ⚠️ Error al procesar oferta de {oferta.get('tienda_nombre', 'desconocida')}: {err_oferta}")

            time.sleep(random.uniform(3.0, 5.0))
        except Exception as err_producto:
            print(f"❌ Error al buscar el producto '{producto.get('nombre', 'desconocido')}': {err_producto}")

    # Guardamos el estado solo para recordar qué ofertas ya fueron notificadas
    guardar_estado(estado)

    print("\nMonitor finalizado correctamente.")


if __name__ == "__main__":
    main()