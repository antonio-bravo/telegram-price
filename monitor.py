import asyncio
import aiohttp
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import random
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# LIBRERÍA CLAVE: Imita la huella digital TLS de Chrome (con fallback seguro)
try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:
    curl_requests = None
    CurlAsyncSession = None

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
    "fecha", "producto_id", "producto", "tienda", "pais",
    "precio_producto", "envio", "envio_confirmado", "total_unidad_con_envio",
    "cantidad", "total_pedido_con_envio", "precio_unidad_medida", "cumple_objetivo", "url",
]

# ============================================================
# CARGA DE CONFIGURACION Y ESTADO
# ============================================================
def cargar_configuracion():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    config.setdefault("solo_union_europea", True)
    if "buscar_en_europa" in config and "solo_union_europea" not in config:
        config["solo_union_europea"] = config["buscar_en_europa"]
    config.setdefault("pais", "España")
    config.setdefault("moneda", "EUR")
    config.setdefault("tasa_pln_eur", 0.235)
    config.setdefault("incluir_wallapop_vinted", True)
    config.setdefault("ignorar_vendidos", True)
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
                    # Limpiamos registros sin alerta previa para no bloquear ofertas futuras
                    datos["ofertas"] = {
                        k: v for k, v in datos["ofertas"].items()
                        if isinstance(v, dict) and v.get("ultima_alerta") is not None
                    }
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
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(estado, f, indent=2, ensure_ascii=False)
            if temporal.exists():
                temporal.unlink()
    except Exception as e:
        print(f"⚠️ Error al guardar estado: {e}")

# ============================================================
# EXTRACCION Y PARSEO DE NUMEROS, PRECIOS Y ENVIOS
# ============================================================
TASA_PLN_A_EUR = 0.235

def convertir_numero(texto):
    if isinstance(texto, (int, float)):
        return float(texto)
    texto = str(texto).replace("\u00a0", " ").replace("&nbsp;", " ")
    texto = re.sub(r'[,.](?:-+|\u2013+|\u2014+)(?:\*)?', ',00', texto)
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

def extraer_precio_amazon(texto):
    if not texto: return None
    # 1. Inputs ocultos oficiales del precio del producto principal de Amazon
    patrones_input = [
        r"""<input[^>]+id=["\x27]twister-plus-price-data-price["\x27][^>]+value=["\x27]([\d.,]+)["\x27]""",
        r"""<input[^>]+name=["\x27]priceValue["\x27][^>]+value=["\x27]([\d.,]+)["\x27]""",
        r"""<input[^>]+name=["\x27]items\[0\.base\]\[customerVisiblePrice\]\[amount\]["\x27][^>]+value=["\x27]([\d.,]+)["\x27]""",
        r"""<input[^>]+id=["\x27]displayedPrice["\x27][^>]+value=["\x27]([\d.,]+)["\x27]""",
    ]
    for p in patrones_input:
        m = re.search(p, texto, re.IGNORECASE)
        if m:
            try:
                val = convertir_numero(m.group(1))
                if 1 <= val <= 100000: return val
            except ValueError: pass

    # 2. JSON de twister-plus-buying-options-price-data
    m_twister = re.search(r"""twister-plus-buying-options-price-data["\x27][^>]*>(\{.*?\})</div>""", texto, re.DOTALL)
    if m_twister:
        m_amt = re.search(r"""["\x27]priceAmount["\x27]\s*:\s*([\d.]+)""", m_twister.group(1))
        if m_amt:
            try:
                val = convertir_numero(m_amt.group(1))
                if 1 <= val <= 100000: return val
            except ValueError: pass

    # 3. Contenedores de precio principal de Amazon (corePrice, apex, buybox)
    for id_contenedor in ["corePriceDisplay_desktop_feature_div", "corePrice_desktop", "apex_desktop", "desktop_buybox", "buybox", "corePrice_feature_div"]:
        pos = texto.find(id_contenedor)
        if pos != -1:
            snippet = texto[pos:pos+4000]
            m_off = re.search(r"""class=["\x27][^"\x27]*a-offscreen[^"\x27]*["\x27][^>]*>([^<]+)</span>""", snippet, re.IGNORECASE)
            if m_off:
                try:
                    val = convertir_numero(m_off.group(1))
                    if 1 <= val <= 100000: return val
                except ValueError: pass
            m_whole = re.search(r"""class=["\x27]a-price-whole["\x27]>(\d+).*?class=["\x27]a-price-fraction["\x27]>(\d+)""", snippet, re.DOTALL | re.IGNORECASE)
            if m_whole:
                try:
                    val = float(f"{m_whole.group(1)}.{m_whole.group(2)}")
                    if 1 <= val <= 100000: return val
                except ValueError: pass

    # 4. Clases directas de precio principal (priceToPay, apex-pricetopay-value)
    for cls in ["priceToPay", "apex-pricetopay-value", "reinventPricePriceToPayMargin"]:
        pos = texto.find(cls)
        if pos != -1:
            snippet = texto[pos:pos+1500]
            m_off = re.search(r"""class=["\x27][^"\x27]*a-offscreen[^"\x27]*["\x27][^>]*>([^<]+)</span>""", snippet, re.IGNORECASE)
            if m_off:
                try:
                    val = convertir_numero(m_off.group(1))
                    if 1 <= val <= 100000: return val
                except ValueError: pass
            m_whole = re.search(r"""class=["\x27]a-price-whole["\x27]>(\d+).*?class=["\x27]a-price-fraction["\x27]>(\d+)""", snippet, re.DOTALL | re.IGNORECASE)
            if m_whole:
                try:
                    val = float(f"{m_whole.group(1)}.{m_whole.group(2)}")
                    if 1 <= val <= 100000: return val
                except ValueError: pass

    # 5. Selectores clásicos de Amazon
    m_pb = re.search(r"""id=["\x27](?:priceblock_ourprice|priceblock_dealprice|price_inside_buybox)["\x27][^>]*>([^<]+)</span>""", texto, re.IGNORECASE)
    if m_pb:
        try:
            val = convertir_numero(m_pb.group(1))
            if 1 <= val <= 100000: return val
        except ValueError: pass

    # 6. Ofertas de otros vendedores (OLP)
    m_olp = re.search(r"""href=["\x27][^"\x27]*(?:/gp/offer-listing/|/dp-olp/)[^"\x27]*["\x27][^>]*>.*?<span[^>]*class=["\x27][^"\x27]*a-offscreen[^"\x27]*["\x27][^>]*>([^<]+)</span>""", texto, re.DOTALL | re.IGNORECASE)
    if m_olp:
        try:
            val = convertir_numero(m_olp.group(1))
            if 1 <= val <= 100000: return val
        except ValueError: pass

    # En Amazon NUNCA usar fallback global para evitar capturar anuncios patrocinados de otros artículos
    return None

def es_articulo_disponible(texto, url):
    if not texto or not url:
        return True
    texto_lower = texto.lower()
    url_lower = url.lower()

    # 1. WALLAPOP
    if "wallapop.com" in url_lower:
        if '"httpcode":404' in texto_lower or '"httpcode": 404' in texto_lower or "no hemos encontrado lo que buscas" in texto_lower:
            return False
        patrones_wallapop_sold = [
            r'["\']sold["\']\s*:\s*true',
            r'["\']reserved["\']\s*:\s*true',
            r'["\']status["\']\s*:\s*["\'](?:sold|reserved|expired|deleted|not_available)["\']',
            r'class=["\'][^"\']*(?:itemdetailstatus|badge|status)[^"\']*["\'][^>]*>\s*(?:vendido|reservado|sold|reserved)\s*<',
            r'<button[^>]*disabled[^>]*>.*?(?:reservado|vendido).*?</button>',
            r'\b(?:este producto ya se ha vendido|artículo no disponible|producto no disponible)\b',
        ]
        for p in patrones_wallapop_sold:
            if re.search(p, texto_lower):
                return False
        return True

    # 2. VINTED
    if "vinted." in url_lower:
        patrones_vinted_sold = [
            r'["\']is_closed["\']\s*:\s*true',
            r'["\']is_reserved["\']\s*:\s*true',
            r'["\']status["\']\s*:\s*["\'](?:sold|closed)["\']',
            r'data-testid=["\']item-status["\'][^>]*>\s*(?:vendido|vendu|sold|verkauft|venduto)\s*<',
            r'\b(?:este artículo está vendido|este artículo está reservado|cet article est vendu|this item is sold)\b',
        ]
        for p in patrones_vinted_sold:
            if re.search(p, texto_lower):
                return False
        return True

    # 3. EBAY
    if "ebay." in url_lower:
        patrones_ebay_sold = [
            r'\b(?:este artículo se ha vendido|el anuncio ha finalizado|este anuncio ha finalizado|this listing was ended|this item is out of stock)\b',
            r'class=["\'][^"\']*(?:ended-status|status-banner)[^"\']*["\'][^>]*>(?:finalizado|vendido|ended|sold)',
        ]
        for p in patrones_ebay_sold:
            if re.search(p, texto_lower):
                return False
        return True

    # 4. AMAZON (Disponibilidad principal)
    if "amazon." in url_lower:
        patron_amz_no_disp = r'id=["\']availability["\'][^>]*>.*?[\'"]?(?:no disponible|agotado|currently unavailable|out of stock|temporarily out of stock|derzeit nicht verfügbar|nicht auf lager|actuellement indisponible|temporairement en rupture|attualmente non disponibile)[\'"]?'
        if re.search(patron_amz_no_disp, texto_lower, re.DOTALL):
            if not re.search(r'/gp/offer-listing/|/dp-olp/', texto_lower):
                return False

    return True

# Retrocompatibilidad
es_articulo_disponible_c2c = es_articulo_disponible

def extraer_precio(texto, url=""):
    if not texto:
        return None
    texto_unescaped = html.unescape(texto)

    # 1. Extractor especializado para Amazon
    if url and ("amazon." in url.lower() or "/dp/" in url.lower()):
        p_amz = extraer_precio_amazon(texto_unescaped)
        if p_amz is not None:
            return p_amz
        return None  # Si Amazon no tiene precio en su buybox/inputs, no inventar con anuncios o carruseles

    # 2. Extractor especializado para Wallapop
    if url and "wallapop.com" in url.lower():
        # JSON en __NEXT_DATA__
        m_w_json = re.search(r'["\']price["\']\s*:\s*\{[^}]*?["\']amount["\']\s*:\s*([\d.]+)', texto_unescaped)
        if m_w_json:
            try:
                p = float(m_w_json.group(1))
                if 1 <= p <= 100000: return p
            except ValueError: pass
        # En el título de la página (ej: 'por 319,95 EUR' o 'per 319,95 EUR')
        m_w_title = re.search(r'(?:por|per|for|pour)\s*([\d.,]+)\s*(?:€|eur)\b', texto_unescaped, re.IGNORECASE)
        if m_w_title:
            try:
                p = float(m_w_title.group(1).replace(",", "."))
                if 1 <= p <= 100000: return p
            except ValueError: pass
        # Elemento DOM de precio
        m_w_dom = re.search(r'class=["\'][^"\']*(?:itemprice|item-detail-price)[^"\']*["\'][^>]*>.*?([\d.,]+)\s*€', texto_unescaped, re.DOTALL | re.IGNORECASE)
        if m_w_dom:
            try:
                p = float(m_w_dom.group(1).replace(",", "."))
                if 1 <= p <= 100000: return p
            except ValueError: pass

    # 3. Extractor especializado para PcComponentes
    if url and "pccomponentes.com" in url.lower():
        m_pcc = re.search(r'id=["\']pdp-price["\'][^>]*>([^\x3c]+)</span>', texto_unescaped, re.IGNORECASE) or re.search(r'id=["\']precio-main["\'][^>]*>.*?<span[^>]*>([^\x3c]+)</span>', texto_unescaped, re.DOTALL | re.IGNORECASE) or re.search(r'data-price=["\']([\d.,]+)["\']', texto_unescaped, re.IGNORECASE)
        if m_pcc:
            try:
                clean = re.sub(r"[^\d.,]", "", m_pcc.group(1).replace(",", "."))
                p = float(clean)
                if 1 <= p <= 100000: return p
            except ValueError: pass

    # 4. Extractor especializado para Geizhals y Guenstiger
    if url and any(x in url.lower() for x in ["geizhals.", "guenstiger."]):
        m_gh = re.search(r'class=["\'][^"\']*(?:gh_price|notrans|offer__price|price-tag)[^"\']*["\'][^>]*>.*?([\d.,]+)\s*€', texto_unescaped, re.DOTALL | re.IGNORECASE) or re.search(r'(?:ab|desde)\s*(?:€\s*)?([\d.,]+)\s*(?:€)?', texto_unescaped, re.IGNORECASE)
        if m_gh:
            try:
                clean = re.sub(r"[^\d.,]", "", m_gh.group(1).replace(",", "."))
                val = float(clean)
                if 1 <= val <= 100000: return val
            except ValueError: pass

    moneda = r"(?:€|eur(?:os)?)"
    texto_limpio = re.sub(rf"\b(?:under|moins de|sous|menos de|unter|less than|sotto|gifts under|regalos por debajo de|cadeaux de moins de)\s+\d+(?:[.,]\d+)?\s*{moneda}(?!\w)", "", texto_unescaped, flags=re.IGNORECASE)
    texto_limpio = re.sub(rf"\b(?:desde\s+)?\d+(?:[.,]\d+)?\s*{moneda}?\s*(?:/|\s*al\s*|\s*por\s*|\s*cada\s*)?(?:mes|m|month|monat)\b", "", texto_limpio, flags=re.IGNORECASE)
    texto_limpio = re.sub(rf"\b\d+\s*(?:cuotas?|plazos?|meses)\s*(?:de)?\s*\d+(?:[.,]\d+)?\s*{moneda}(?!\w)", "", texto_limpio, flags=re.IGNORECASE)
    texto_limpio = re.sub(rf"\b(?:ahorra|ahorro|descuento|rebaja|save|rabatt|remise|économisez)\s*(?:de|hasta|up to|jusqu'à)?\s*\d+(?:[.,]\d+)?\s*{moneda}(?!\w)", "", texto_limpio, flags=re.IGNORECASE)
    texto_limpio = re.sub(rf"\b(?:antes|pvp|precio anterior|precio recomendado|was|statt|au lieu de)\s*:\s*\d+(?:[.,]\d+)?\s*{moneda}(?!\w)", "", texto_limpio, flags=re.IGNORECASE)

    # 5. JSON-LD estructurado
    for m in re.finditer(r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>', texto_limpio, re.DOTALL | re.IGNORECASE):
        try:
            datos_ld = json.loads(m.group(1))
            bloques = []
            if isinstance(datos_ld, list): bloques.extend(datos_ld)
            elif isinstance(datos_ld, dict):
                if "@graph" in datos_ld and isinstance(datos_ld["@graph"], list): bloques.extend(datos_ld["@graph"])
                else: bloques.append(datos_ld)
            for b in bloques:
                if not isinstance(b, dict): continue
                offers = b.get("offers")
                lista_ofertas = []
                if isinstance(offers, list): lista_ofertas.extend(offers)
                elif isinstance(offers, dict):
                    if isinstance(offers.get("offers"), list): lista_ofertas.extend(offers["offers"])
                    lista_ofertas.append(offers)
                elif "price" in b or "lowPrice" in b: lista_ofertas.append(b)
                for off in lista_ofertas:
                    if not isinstance(off, dict): continue
                    avail = str(off.get("availability", "")).lower()
                    if any(s in avail for s in ["outofstock", "soldout", "discontinued"]): continue
                    val = off.get("price") or off.get("lowPrice") or off.get("highPrice")
                    if val is None:
                        ps = off.get("priceSpecification")
                        if isinstance(ps, dict): val = ps.get("price") or ps.get("minPrice")
                        elif isinstance(ps, list) and ps: val = ps[0].get("price") if isinstance(ps[0], dict) else None
                    if val is not None:
                        try:
                            precio_ld = convertir_numero(val)
                            curr = str(off.get("priceCurrency", "")).upper()
                            if curr == "PLN": precio_ld = round(precio_ld * TASA_PLN_A_EUR, 2)
                            if 1 <= precio_ld <= 100000: return precio_ld
                        except ValueError: pass
        except Exception: pass

    # 6. Metadatos HTML
    m_meta = re.search(r'<meta[^>]+(?:property|name)=[\'"][^\'"]*(?:price:amount|data1|price)[\'"][^>]+content=[\'"]([^\'"]+)[\'"]', texto_limpio, re.IGNORECASE) or re.search(r'<meta[^>]+content=[\'"]([^\'"]+)[\'"][^>]+(?:property|name)=[\'"][^\'"]*(?:price:amount|data1|price)[\'"]', texto_limpio, re.IGNORECASE)
    if m_meta:
        try:
            p = convertir_numero(m_meta.group(1))
            if 1 <= p <= 100000: return p
        except ValueError: pass

    m_itemprop = re.search(r'<[^>]+itemprop=[\'"](?:price|lowPrice|highPrice)[\'"][^>]*content=[\'"]([^\'"]+)[\'"]', texto_limpio, re.IGNORECASE) or re.search(r'<[^>]+content=[\'"]([^\'"]+)[\'"][^>]*itemprop=[\'"](?:price|lowPrice|highPrice)[\'"]', texto_limpio, re.IGNORECASE)
    if m_itemprop:
        try:
            p = convertir_numero(m_itemprop.group(1))
            if 1 <= p <= 100000: return p
        except ValueError: pass

    # 7. MediaMarkt, PcComponentes, Next.js y JSON de compra
    m_mm = re.search(r'<span[^>]*data-test=[\'"]branded-price[\'"][^>]*>([^<]+)</span>', texto_limpio, re.IGNORECASE)
    if m_mm:
        try:
            p = convertir_numero(m_mm.group(1))
            if 1 <= p <= 100000: return p
        except ValueError: pass

    m_mf = re.search(r'<[^>]+class=[\'"][^\'"]*(?:\bpprice\b|\bivprice\b)[^\'"]*[\'"][^>]*>([^<]+)<', texto_limpio, re.IGNORECASE) or re.search(r'<span[^>]+id=[\'"][^\'"]*price[^\'"]*[\'"][^>]*>([^<]+)<', texto_limpio, re.IGNORECASE)
    if m_mf:
        try:
            p = convertir_numero(m_mf.group(1))
            if 1 <= p <= 100000: return p
        except ValueError: pass

    json_price = re.search(r'[\'"](?:priceToPay|buyingPrice|priceAmount|currentPrice|salesPrice|amountInclusive|displayPrice)[\'"]\s*:\s*\{?[^}]*?[\'"]?(?:amount|value)?[\'"]?\s*:\s*[\'"]?([\d.,]+)[\'"]?', texto_limpio, re.IGNORECASE)
    if json_price:
        try:
            p = convertir_numero(json_price.group(1))
            if 1 <= p <= 100000: return p
        except ValueError: pass

    m_next = re.search(r"""<script[^>]*id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""", texto_limpio, re.DOTALL | re.IGNORECASE)
    if m_next:
        m_cash = re.search(r"""["'](?:cash|price|salesPrice|currentPrice)["']\s*:\s*\{?[^}]*?["'](?:amount|value)?["']?\s*:\s*([\d.]+)""", m_next.group(1)) or re.search(r"""["'](?:cash|price)["']\s*:\s*\{?[^}]*?["']amount["']\s*:\s*([\d.]+)""", m_next.group(1))
        if m_cash:
            try:
                p = float(m_cash.group(1))
                if 1 <= p <= 100000: return p
            except ValueError: pass

    # 8. FALLBACK GENERAL EN TEXTO VISIBLE (Eliminando scripts y estilos para no leer anuncios de JavaScript)
    texto_sin_scripts = re.sub(r'<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>', '', texto_limpio, flags=re.IGNORECASE)
    texto_sin_scripts = re.sub(r'<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>', '', texto_sin_scripts, flags=re.IGNORECASE)
    texto_visible = re.sub(r'<[^>]+>', ' ', texto_sin_scripts)

    patrones = [
        r"(\d{1,3}(?:[.,]\d{3})*(?:[.,](?:\d{1,2}|-+|\u2013+))?)\s*(?:\*|\s)*€",
        r"€\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,](?:\d{1,2}|-+|\u2013+))?)",
    ]
    for patron in patrones:
        for coincidencia in re.finditer(patron, texto_visible, re.IGNORECASE):
            try:
                precio = convertir_numero(coincidencia.group(1))
                if 1 <= precio <= 100000: return precio
            except ValueError: continue
    return None

def extraer_envio(texto):
    texto_lower = texto.lower()
    terminos_gratis = ["envío gratis", "envio gratis", "envío gratuito", "free shipping", "kostenloser versand", "livraison gratuite", "spedizione gratuita", "gratis verzending", "darmowa dostawa"]
    for termino in terminos_gratis:
        if termino in texto_lower: return 0.0
    patrones = [
        r"(?:gastos de |coste de )?env[ií]o\s*(?:desde|de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"(?:shipping|delivery)(?:\s*to\s*spain)?\s*(?:from|:)?\s*€?\s*(\d+(?:[.,]\d{1,2})?)",
        r"(?:versand|lieferung)(?:\s*nach\s*spanien)?\s*(?:ab|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
    ]
    for patron in patrones:
        coincidencia = re.search(patron, texto_lower)
        if coincidencia:
            try: return convertir_numero(coincidencia.group(1))
            except ValueError: pass
    return None

def extraer_capacidad(texto, unidad):
    texto = texto.lower()
    unidad = unidad.lower()
    patrones = {"tb": r"(\d+(?:[.,]\d+)?)\s*tb", "gb": r"(\d+(?:[.,]\d+)?)\s*gb", "kg": r"(\d+(?:[.,]\d+)?)\s*kg"}
    patron = patrones.get(unidad)
    if not patron: return None
    coincidencia = re.search(patron, texto)
    if not coincidencia: return None
    try: return convertir_numero(coincidencia.group(1))
    except ValueError: return None

# ============================================================
# PAISES Y TIENDAS DE LA UNION EUROPEA
# ============================================================
PAISES_UE_TLD = {"es", "de", "fr", "it", "pt", "nl", "be", "at", "pl", "se", "dk", "ie", "fi", "cz", "gr", "eu"}
DOMINIOS_FUERA_UE = (".co.uk", ".uk", ".ch", ".us", ".ca", ".au", ".cn", ".jp", ".ru", "amazon.com", "amazon.co.uk", "amazon.ca", "amazon.com.au", "ebay.com", "ebay.co.uk", "idealo.co.uk", "scan.co.uk", "ebuyer.com", "overclockers.co.uk", "box.co.uk", "vinted.co.uk")
DOMINIOS_IGNORADOS = ("bing.com", "google.com", "yahoo.com", "duckduckgo.com", "doubleclick.net", "yandex.com", "baidu.com", "msn.com")

def es_tienda_ue(url):
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if any(ign in domain for ign in DOMINIOS_IGNORADOS): return False
    if domain.endswith(DOMINIOS_FUERA_UE) or any(d in domain for d in DOMINIOS_FUERA_UE): return False
    if domain.startswith(("uk.", "us.", "ca.", "au.")): return False
    partes = domain.split(".")
    tld = partes[-1] if partes else ""
    if tld in PAISES_UE_TLD: return True
    tiendas_ue_conocidas = ["amazon.es", "amazon.de", "amazon.fr", "amazon.it", "amazon.nl", "amazon.com.be", "wallapop", "vinted", "hoteudeals", "geizhals", "idealo", "guenstiger", "cenypc", "tagdip", "priceinfo", "pccomponentes", "mediamarkt", "coolmod", "neobyte", "aussar", "senetic", "europeanpc", "informaticabarata", "shopdutyfree", "alternate", "caseking", "mindfactory", "playox", "office-partner", "galaxus", "joybuy", "megekko", "yorcom", "allegro", "proline", "komputronik", "soloperaffare", "ldlc"]
    return any(t in domain for t in tiendas_ue_conocidas)

def detectar_tienda_y_pais(url):
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if "wallapop" in domain:
        if domain.startswith("it."): return "Wallapop", "🇮🇹 Wallapop Italia (2ª mano)", True
        if domain.startswith("pt."): return "Wallapop", "🇵🇹 Wallapop Portugal (2ª mano)", True
        return "Wallapop", "🇪🇸 Wallapop (2ª mano)", False
    if "vinted" in domain: return "Vinted", "🇪🇺 Vinted (2ª mano)", True
    if "amazon.es" in domain: return "Amazon España", "🇪🇸 España", False
    if "amazon.de" in domain: return "Amazon Alemania", "🇩🇪 Alemania", True
    if "amazon.fr" in domain: return "Amazon Francia", "🇫🇷 Francia", True
    if "amazon.it" in domain: return "Amazon Italia", "🇮🇹 Italia", True
    if "amazon.nl" in domain: return "Amazon Países Bajos", "🇳🇱 Países Bajos", True
    if "amazon.com.be" in domain or "amazon.be" in domain: return "Amazon Bélgica", "🇧🇪 Bélgica", True
    if "hoteudeals" in domain: return "HotEUDeals", "🇪🇺 HotEUDeals (Europa)", True
    if "geizhals" in domain: return "Geizhals (Europa)", "🇪🇺 Europa (UE)", True
    if "guenstiger" in domain: return "Guenstiger", "🇩🇪 Alemania", True
    if "cenypc" in domain: return "CenyPC", "🇵🇱 Polonia", True
    if "idealo" in domain:
        if domain.endswith(".de"): return "Idealo Alemania", "🇩🇪 Alemania", True
        if domain.endswith(".fr"): return "Idealo Francia", "🇫🇷 Francia", True
        if domain.endswith(".it"): return "Idealo Italia", "🇮🇹 Italia", True
        return "Idealo", "🇪🇺 Europa (UE)", True
    if "mindfactory" in domain: return "Mindfactory", "🇩🇪 Alemania", True
    if "playox" in domain: return "Playox", "🇩🇪 Alemania", True
    if "galaxus" in domain: return "Galaxus", "🇩🇪 Alemania", True
    if "megekko" in domain: return "Megekko", "🇳🇱 Países Bajos", True
    if "allegro" in domain: return "Allegro", "🇵🇱 Polonia", True
    if "mediamarkt" in domain:
        if domain.endswith(".de"): return "MediaMarkt Alemania", "🇩🇪 Alemania", True
        return "MediaMarkt", "🇪🇸 España", False
    if "pccomponentes" in domain: return "PcComponentes", "🇪🇸 España", False
    if domain.endswith((".co.uk", ".uk")) or "amazon.co.uk" in domain: return domain, "🇬🇧 Reino Unido (Fuera UE)", True
    if domain.endswith(".es"): return domain, "🇪🇸 España", False
    if domain.endswith(".de"): return domain, "🇩🇪 Alemania", True
    if domain.endswith(".pl"): return domain, "🇵🇱 Polonia", True
    if domain.endswith(".nl"): return domain, "🇳🇱 Países Bajos", True
    if domain.endswith(".it"): return domain, "🇮🇹 Italia", True
    if domain.endswith(".fr"): return domain, "🇫🇷 Francia", True
    return domain, "🇪🇺 Europa (UE)", True

def detectar_condicion(url, titulo="", texto_html=""):
    texto_eval = f"{titulo}".lower()
    url_lower = url.lower()
    if "wallapop" in url_lower or "vinted" in url_lower: return "Segunda mano (Wallapop/Vinted)"
    palabras_reacondicionado = ["reacondicionado", "reacondicionada", "renewed", "refurbished", "segunda mano", "2ª mano", "d'occasion", "gebraucht", "usato", "outlet", "como nuevo", "b-ware"]
    if any(p in texto_eval for p in palabras_reacondicionado) or any(p in url_lower for p in ["reacondicionado", "renewed", "warehouse", "outlet"]): return "Reacondicionado / Segunda mano"
    return "Nuevo"

def normalizar_texto(texto):
    if not texto: return ""
    t = unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8').lower()
    return t.replace(",", ".")

# ============================================================
# VALIDACION Y FILTRADO DE RESULTADOS
# ============================================================
def es_resultado_valido(producto, titulo, descripcion, precio, config, url=""):
    texto = f"{titulo} {descripcion} {url}".lower()
    if config.get("solo_union_europea", True):
        if not es_tienda_ue(url): return False
        if "£" in texto or "gbp" in texto or "$" in texto or "usd" in texto: return False
    
    es_marketplace_c2c = "wallapop" in url.lower() or "vinted" in url.lower()
    if es_marketplace_c2c:
        descartes_c2c = ["vendido", "vendida", "reservado", "reservada", "no disponible", "ya no está", "sold", "sold out", "reserved", "no longer available", "verkauft", "reserviert", "nicht verfügbar", "vendu", "vendue", "réservé", "non disponible", "venduto", "venduta", "riservato", "para piezas", "averiado", "no funciona", "roto", "defectuoso", "sin probar", "bloqueado", "cable", "carcasa", "caddy", "funda", "dock", "adaptador"]
        for termino in descartes_c2c:
            if termino in texto: return False
        if config.get("ignorar_vendidos", True):
            patron_antiguo = re.search(r'\b(?:hace\s+\d+\s+(?:mes(?:es)?|año(?:s)?|sem(?:ana)?s?)|\d+\s+(?:months?|weeks?|years?)\s+ago|il y a\s+\d+\s+(?:mois|semaines?|ans?)|vor\s+\d+\s+(?:monaten?|wochen?|jahren?))\b', texto, re.IGNORECASE)
            if patron_antiguo: return False
    else:
        exclusiones = producto.get("excluir_terminos") if producto.get("excluir_terminos") is not None else config.get("excluir_terminos", TERMINOS_EXCLUSION_DEFECTO)
        for termino in exclusiones:
            term_clean = termino.lower().strip()
            # Si el término es un accesorio común, evitar descartar productos principales que lo incluyan (ej. mandos con cable incluido)
            if term_clean in ("cable", "funda", "carcasa", "caddy", "dock", "adaptador"):
                patron_accesorio_solo = rf'\b(?:solo|pack de|para|for|de)\s+{re.escape(term_clean)}\b|^\s*{re.escape(term_clean)}\b'
                if re.search(patron_accesorio_solo, titulo.lower()):
                    return False
            else:
                if re.search(r'\b' + re.escape(term_clean) + r'\b', texto):
                    return False

    texto_raw = f"{titulo} {url}".replace(",", ".")
    texto_titulo_url = normalizar_texto(texto_raw)
    texto_norm = re.sub(r'[^a-z0-9]', '', texto_titulo_url)

    requeridos = producto.get("terminos_requeridos")
    if requeridos:
        tokens_a_validar = [str(r) for r in requeridos]
    else:
        tokens_a_validar = producto.get("busqueda", "").split()

    stopwords_cortas = {"de", "del", "en", "el", "la", "los", "las", "un", "una", "al", "da", "di", "in", "of", "for", "with", "to", "at", "zu", "im", "am", "und", "y", "e", "-", "/", "&", "+"}

    for token in tokens_a_validar:
        token_clean = normalizar_texto(token).strip()
        if not token_clean or token_clean in stopwords_cortas:
            continue
        token_norm = re.sub(r'[^a-z0-9]', '', token_clean)
        modelo_base = token_clean.split("-")[0]
        modelo_base_norm = re.sub(r'[^a-z0-9]', '', modelo_base)

        coincide = (
            token_clean in texto_titulo_url or
            (token_norm and token_norm in texto_norm) or
            (modelo_base and modelo_base in texto_titulo_url) or
            (modelo_base_norm and modelo_base_norm in texto_norm)
        )
        if not coincide:
            return False

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
        coincide_cap = (cap_str in texto or cap_str_space in texto or cap_str_dash in texto or (cap_fr and (cap_fr in texto or cap_fr_space in texto or cap_fr_dash in texto)) or modelo_en_texto)
        if not coincide_cap: return False

    precio_min = producto.get("precio_minimo_unidad")
    if precio_min is None:
        max_u = producto.get("precio_maximo_unidad")
        if max_u: precio_min = float(max_u) * 0.25
    if precio_min is not None and precio < float(precio_min): return False
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
            origen_envio = f"{coste_envio:.2f} € (Envío + Protección estimado)"
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
    return {"precio_base": precio, "envio": envio, "envio_conocido": envio_conocido, "es_internacional": es_internacional, "es_c2c": es_c2c, "coste_envio_aplicado": coste_envio, "origen_envio": origen_envio, "cantidad": 1 if es_c2c else cantidad, "total_unidad": total_unidad, "total_pedido": total_pedido}

def calcular_precio_medida(producto, total_unidad):
    limite = producto.get("precio_maximo_por_unidad_medida")
    if limite is None: return None
    medida = producto.get("unidad_medida")
    if not medida: return None
    capacidad = producto.get("capacidad") or producto.get("capacidad_por_unidad")
    if capacidad is None: capacidad = extraer_capacidad(f"{producto.get('busqueda', '')} {producto.get('nombre', '')}", medida)
    if not capacidad or capacidad <= 0: return None
    return total_unidad / float(capacidad)

def evaluar_oferta(producto, costes, precio_medida, config):
    incluir_envio = config.get("incluir_envio", True)
    exigir_confirmado = config.get("exigir_envio_confirmado", False)
    if incluir_envio and exigir_confirmado and not costes["envio_conocido"]:
        return {"cumple": False, "motivo_descarte": "Gastos de envío no confirmados en el texto", "precio_medida": precio_medida, **costes}
    criterios_definidos = 0
    criterios_cumplidos = 0
    max_unidad = producto.get("precio_maximo_unidad")
    if max_unidad is not None:
        criterios_definidos += 1
        if costes["total_unidad"] <= float(max_unidad): criterios_cumplidos += 1
    max_pedido = producto.get("precio_maximo_pedido")
    if max_pedido is not None:
        criterios_definidos += 1
        if costes["total_pedido"] <= float(max_pedido): criterios_cumplidos += 1
    max_medida = producto.get("precio_maximo_por_unidad_medida")
    if max_medida is not None:
        criterios_definidos += 1
        if precio_medida is not None and precio_medida <= float(max_medida): criterios_cumplidos += 1
    cumple = (criterios_definidos > 0) and (criterios_cumplidos == criterios_definidos)
    return {"cumple": cumple, "motivo_descarte": None if cumple else "No cumple los límites de precio definidos", "precio_medida": precio_medida, **costes}

# ============================================================
# HISTORICO CSV Y TELEGRAM
# ============================================================
def guardar_historico(producto, oferta, evaluacion):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    existe = HISTORY_FILE.exists()
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS_CSV)
        if not existe or HISTORY_FILE.stat().st_size == 0: escritor.writeheader()
        escritor.writerow({
            "fecha": datetime.now(timezone.utc).isoformat(), "producto_id": producto["id"], "producto": producto["nombre"],
            "tienda": oferta["tienda_nombre"], "pais": oferta["pais"], "precio_producto": f"{oferta['precio']:.2f}",
            "envio": evaluacion["origen_envio"], "envio_confirmado": "SI" if evaluacion["envio_conocido"] else "NO",
            "total_unidad_con_envio": f"{evaluacion['total_unidad']:.2f}", "cantidad": evaluacion["cantidad"],
            "total_pedido_con_envio": f"{evaluacion['total_pedido']:.2f}",
            "precio_unidad_medida": f"{evaluacion['precio_medida']:.2f}" if evaluacion["precio_medida"] is not None else "",
            "cumple_objetivo": "SI" if evaluacion["cumple"] else "NO", "url": oferta["url"],
        })

def obtener_credenciales_telegram(config=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("bot_token")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or os.getenv("chat_id")
    if not token or not chat_id:
        if config is None:
            try: config = cargar_configuracion()
            except Exception: config = {}
        if config and isinstance(config, dict):
            if not token: token = config.get("telegram_bot_token") or config.get("bot_token")
            if not chat_id: chat_id = config.get("telegram_chat_id") or config.get("chat_id")
    return (str(token).strip() if token else None), (str(chat_id).strip() if chat_id else None)

def enviar_telegram(mensaje, config=None):
    token, chat_id = obtener_credenciales_telegram(config)
    if not token or not chat_id:
        print(f"  ⚠️ No se puede enviar alerta. Faltan credenciales de Telegram.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensaje, "link_preview_options": {"is_disabled": False}}
    try:
        respuesta = requests.post(url, json=payload, timeout=20)
        respuesta.raise_for_status()
        return respuesta.json().get("ok", False)
    except requests.RequestException as error:
        print(f"  ❌ Error enviando mensaje a Telegram: {error}")
        return False

def crear_mensaje(producto, oferta, evaluacion, bajada=False):
    moneda = "€"
    titulo = "📉 BAJADA DE PRECIO" if bajada else "🛒 NUEVA OFERTA"
    tienda = oferta.get("tienda_nombre", oferta["tienda"])
    pais = oferta.get("pais", "🇪🇺 Europa")
    condicion = oferta.get("condicion", "Nuevo")
    estado_str = f"♻️ {condicion}" if any(x in condicion.lower() for x in ["reacondicionado", "segunda mano", "renewed"]) else "🆕 Nuevo"
    mensaje = f"{titulo}\n\n📦 {producto['nombre']}\n🏪 Tienda: {tienda} ({pais})\n🏷️ Estado: {estado_str}\n\n💶 Precio producto: {oferta['precio']:.2f} {moneda}\n🚚 Gastos de envío a España: {evaluacion['origen_envio']}\n💰 Total unidad (con envío): {evaluacion['total_unidad']:.2f} {moneda}\n🔢 Cantidad pedido: {evaluacion['cantidad']}\n🧾 Total pedido (con envío): {evaluacion['total_pedido']:.2f} {moneda}\n"
    if evaluacion["precio_medida"] is not None:
        mensaje += f"📊 Coste por {producto.get('unidad_medida', '')}: {evaluacion['precio_medida']:.2f} {moneda}/{producto.get('unidad_medida', '')}\n"
    if evaluacion["es_internacional"]: mensaje += "\nℹ️ Tienda europea con envío a España. Verifica el importe final en el carrito.\n"
    elif not evaluacion["envio_conocido"]: mensaje += "\n⚠️ Gastos de envío estimados. Verifica antes de comprar.\n"
    mensaje += f"\n🔗 ENLACE A LA OFERTA:\n{oferta['url']}"
    return mensaje

def clave_oferta(producto, oferta):
    return hashlib.sha256(f"{producto['id']}|{oferta['url']}".encode("utf-8")).hexdigest()

def procesar_oferta(producto, oferta, config, estado):
    costes = calcular_costes(producto, oferta["precio"], oferta["envio"], config, es_internacional=oferta["es_internacional"], url=oferta["url"])
    precio_medida = calcular_precio_medida(producto, costes["total_unidad"])
    evaluacion = evaluar_oferta(producto, costes, precio_medida, config)
    guardar_historico(producto, oferta, evaluacion)
    clave = clave_oferta(producto, oferta)
    ofertas_guardadas = estado.setdefault("ofertas", {})
    anterior = ofertas_guardadas.get(clave)
    precio_actual = evaluacion["total_unidad"]

    alerta_previa_activa = (anterior is not None and anterior.get("ultima_alerta") is not None)
    precio_anterior = float(anterior["precio"]) if (anterior is not None and anterior.get("precio") is not None) else None

    avisar = False
    es_bajada = False
    ha_subido = False

    if evaluacion["cumple"]:
        if not alerta_previa_activa:
            # Oferta nueva o previamente invalidada porque el precio había subido / no cumplía
            avisar = True
        else:
            # Ya fue notificada previamente y sigue en precio objetivo
            ha_bajado = precio_anterior is not None and precio_actual < precio_anterior
            ha_subido = precio_anterior is not None and precio_actual > precio_anterior

            if ha_bajado and config.get("notificar_bajadas", True):
                avisar = True
                es_bajada = True
            elif ha_subido:
                # El precio subió pero aún sigue dentro del precio deseado:
                # Actualizamos el precio registrado para detectar futuras bajadas a partir de este nuevo nivel
                ofertas_guardadas[clave]["precio"] = precio_actual
                print(f"  ℹ️ [{oferta['pais']}] {oferta['tienda_nombre']}: El precio subió de {precio_anterior:.2f} € a {precio_actual:.2f} € (sigue cumpliendo). Precio actualizado en estado.")
            elif not config.get("solo_nuevas_ofertas", True) and not ha_bajado:
                avisar = True

        if avisar:
            tipo = "Bajada de precio" if es_bajada else "Oferta en precio deseado"
            print(f"  🔔 ¡{tipo}! Enviando a Telegram: {evaluacion['total_unidad']:.2f} €/unidad ({oferta['tienda_nombre']})...")
            if enviar_telegram(crear_mensaje(producto, oferta, evaluacion, es_bajada), config=config):
                print("  ✅ Notificación de Telegram enviada con éxito.")
                ofertas_guardadas[clave] = {
                    "precio": precio_actual,
                    "ultima_alerta": datetime.now(timezone.utc).isoformat()
                }
            else:
                print("  ❌ No se pudo enviar la notificación a Telegram.")
        else:
            if not ha_subido:
                print(f"  ℹ️ [{oferta['pais']}] {oferta['tienda_nombre']} ({precio_actual:.2f} €): Ya notificada previamente sin variación a la baja.")
    else:
        # NO CUMPLE EL OBJETIVO DE PRECIO
        # Si la oferta estaba en el histórico/estado con alerta previa, queda INVALIDADA
        # ya que los precios cambian y ya no está al precio deseado.
        # Al invalidarla, cuando vuelva a estar a un precio deseado podrá notificarse nuevamente.
        if clave in ofertas_guardadas:
            ofertas_guardadas.pop(clave, None)
            if alerta_previa_activa:
                print(f"  🔄 [{oferta['pais']}] {oferta['tienda_nombre']}: El precio ({precio_actual:.2f} €) ya no cumple objetivo. Histórico/alerta previa invalidada.")

        if evaluacion.get("motivo_descarte"):
            print(f"  ℹ️ [{oferta['pais']}] {oferta['tienda_nombre']} ({oferta['precio']:.2f} €): {evaluacion['motivo_descarte']}")

# ============================================================
# GENERACION DE CONSULTAS Y BUSQUEDA
# ============================================================
def generar_consultas(producto, config):
    busqueda = producto.get("busqueda", "").strip()
    buscar_europa = config.get("solo_union_europea", True)
    consultas = []

    # Limpiar guiones o símbolos sueltos de la búsqueda para la consulta al buscador
    termino = re.sub(r'\s+-\s+', ' ', busqueda).strip()
    termino = re.sub(r'\s+', ' ', termino)
    termino_c2c = termino

    # Consultas más simples y efectivas para DuckDuckGo
    if buscar_europa:
        consultas.append((f'{termino} site:amazon.de OR site:amazon.it OR site:amazon.es', "es-es"))
        consultas.append((f'{termino} site:idealo.de OR site:idealo.es OR site:geizhals.de OR site:guenstiger.de', "es-es"))
        consultas.append((f'{termino} site:pccomponentes.com OR site:mediamarkt.es OR site:mediamarkt.de', "es-es"))
    
    if config.get("incluir_wallapop_vinted", True):
        consultas.append((f'{termino_c2c} site:wallapop.com OR site:vinted.es -vendido -reservado ("€" OR "EUR")', "es-es"))
    
    # Consulta general de respaldo
    consultas.append((f'{termino} precio OR price OR preis ("€" OR "EUR")', "es-es"))

    vistos = set()
    return [(c, reg) for c, reg in consultas if c not in vistos and not vistos.add(c)]

def buscar_en_ddgs(buscador, consulta, region):
    for backend in ["auto", "html", "lite"]:
        try:
            res = buscador.text(consulta, region=region, backend=backend, max_results=10)
            if res: return list(res)
        except Exception as e:
            if "429" in str(e).lower() or "ratelimit" in str(e).lower() or "202" in str(e).lower():
                time.sleep(3.0)
            continue
    return []

def canonicalizar_url(url):
    if not url: return url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")
        if "amazon." in domain:
            m_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', parsed.path)
            if m_asin: return f"https://www.{domain}/dp/{m_asin.group(1)}"
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:
        return url

def clave_deduplicacion_url(url):
    if not url: return url
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")
        if "amazon." in domain:
            m_asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', parsed.path)
            if m_asin: return f"amazon:{domain}:{m_asin.group(1)}"
        if "wallapop.com" in domain:
            m_w = re.search(r'/item/(?:.*-)?(\d+)', parsed.path)
            if m_w: return f"wallapop:{m_w.group(1)}"
        if "vinted." in domain:
            m_v = re.search(r'/items/(\d+)', parsed.path)
            if m_v: return f"vinted:{m_v.group(1)}"
        if "ebay." in domain:
            m_e = re.search(r'/itm/(?:.*-)?(\d+)', parsed.path)
            if m_e: return f"ebay:{m_e.group(1)}"
        return canonicalizar_url(url)
    except Exception:
        return url

def es_url_producto_valida(url, domain=None):
    if not url:
        return False
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if not domain:
            domain = parsed.netloc.lower().removeprefix("www.")
        else:
            domain = domain.lower().removeprefix("www.")

        dominios_no_tienda = [
            "support.", "techcommunity.", "answers.", "wikipedia.org",
            "youtube.com", "reddit.com", "twitter.com", "facebook.com",
            "instagram.com", "tiktok.com", "vuelos.", "fluege.", "flights.",
            "specials.", "newsletter."
        ]
        if any(no_tienda in domain for no_tienda in dominios_no_tienda):
            return False

        # Descartar páginas de búsqueda, categorías, login, carrito, etc.
        patrones_no_producto = [
            "/search", "/suche", "/buscar", "/recherche", "/zoek",
            "/category/", "/categoria/", "/kategorie/", "/categories/",
            "/cart", "/carrito", "/warenkorb", "/panier", "/basket",
            "/checkout", "/account", "/cuenta", "/login", "/signin",
            "/help", "/ayuda", "/faq", "/terms", "/privacy", "/privacidad",
            "/blog", "/news", "/noticias", "/forum", "/foro", "/community"
        ]
        if any(p in path for p in patrones_no_producto):
            return False

        # Descartar páginas raíz o sin ruta
        if path in ("", "/"):
            return False

        # Validaciones específicas por marketplace/tienda
        if "amazon." in domain:
            return bool(re.search(r'/(?:dp|gp/product)/[A-Z0-9]{10}', parsed.path, re.IGNORECASE))

        if "wallapop.com" in domain:
            return "/item/" in path and bool(re.search(r'/item/(?:.*-)?\d+', path))

        if "vinted." in domain:
            return "/items/" in path and bool(re.search(r'/items/\d+', path))

        if "ebay." in domain:
            return "/itm/" in path

        if "idealo." in domain:
            if "/preisvergleich/liste/" in path:
                return False
            return True

        return True
    except Exception:
        return False

def es_entorno_datacenter():
    modo_res = os.getenv("MODO_RESIDENCIAL", "").strip().lower()
    if modo_res in ["true", "1", "si", "yes"]: return False
    if modo_res in ["false", "0", "no"]: return True
    return (os.getenv("GITHUB_ACTIONS") == "true") or (os.getenv("CI") == "true")

def es_tienda_bloqueada_en_datacenter(domain):
    return any(d in domain for d in ("amazon.", "wallapop.", "vinted."))

# ============================================================
# MOTOR ASÍNCRONO CON PLAYWRIGHT (NUEVO)
# ============================================================
# ============================================================
# MOTOR ASÍNCRONO CON PLAYWRIGHT (TOLERANTE A FALLOS)
# ============================================================
class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.disponible = False
        # Limitar concurrencia estricta a 2 páginas para no saturar CPU en GitHub Actions
        self.semaphore = asyncio.Semaphore(2)

    async def start(self):
        try:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
            )
            self.disponible = True
            print("✅ Motor de navegador (Playwright) iniciado correctamente.")
        except Exception as e:
            print(f"⚠️ Playwright no disponible o falló al iniciar: {e}")
            print("   ℹ️ Se usará únicamente el método de petición rápida.")
            self.disponible = False

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def get_page_content(self, url):
        if not self.disponible or not self.browser:
            return None, None
        async with self.semaphore:
            context = await self.browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="es-ES",
                timezone_id="Europe/Madrid",
                viewport={'width': 1920, 'height': 1080}
            )
            page = await context.new_page()
            try:
                # Bloquear descargas pesadas (imágenes, medios, fuentes) para carga ultrarrápida y evitar timeouts
                await page.route(
                    "**/*",
                    lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_()
                )
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                await page.goto(url, wait_until='domcontentloaded', timeout=25000)
                await asyncio.sleep(1.5)
                return await page.content(), await page.title()
            except Exception as e:
                print(f"   ⚠️ Playwright falló en {urlparse(url).netloc}: {type(e).__name__} ({e})")
                return None, None
            finally:
                await context.close()

async def consultar_url_async(url, producto, config, browser_mgr, semaphore):
    async with semaphore:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
        texto = None

        # 1. Petición rápida con curl_cffi (imita huella TLS de Chrome para saltar bloqueos de Cloudflare/Amazon)
        if CurlAsyncSession is not None:
            try:
                async with CurlAsyncSession(impersonate="chrome124", timeout=12) as c_session:
                    resp = await c_session.get(url, headers=headers)
                    if resp.status_code == 200:
                        texto = resp.text
            except Exception:
                texto = None

        # Fallback de petición rápida con aiohttp si curl_cffi no estaba disponible o dio error
        if not texto:
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers, allow_redirects=True) as response:
                        if response.status == 200:
                            texto = await response.text(errors="replace")
            except Exception:
                texto = None

        if texto:
            if not es_articulo_disponible_c2c(texto, url):
                print(f"   🚫 Artículo no disponible o ya vendido en {urlparse(url).netloc}: {url}")
                return None
            precio = extraer_precio(texto, url)
            if precio:
                titulo = producto["nombre"]
                m_title = re.search(r'<title[^>]*>(.*?)</title>', texto, re.IGNORECASE | re.DOTALL)
                if m_title: titulo = html.unescape(m_title.group(1)).strip()
                tienda_nombre, pais, es_inter = detectar_tienda_y_pais(url)
                return {
                    "producto_id": producto["id"], "producto": producto["nombre"],
                    "tienda": urlparse(url).netloc.lower().removeprefix("www."),
                    "tienda_nombre": tienda_nombre, "pais": pais, "es_internacional": es_inter,
                    "precio": precio, "envio": extraer_envio(texto), "url": url,
                    "titulo": titulo, "condicion": detectar_condicion(url, titulo, texto),
                }

        # 2. FALLBACK Playwright si la petición HTTP directa no devolvió precio
        if browser_mgr.disponible:
            print(f"   🔄 Fallo/Price missing en {urlparse(url).netloc}. Usando navegador real (Playwright)...")
            html_content, titulo_real = await browser_mgr.get_page_content(url)
            if html_content:
                if not es_articulo_disponible_c2c(html_content, url):
                    print(f"   🚫 Artículo no disponible o ya vendido en {urlparse(url).netloc}: {url}")
                    return None
                precio_pw = extraer_precio(html_content, url)
                if precio_pw:
                    tienda_nombre, pais, es_inter = detectar_tienda_y_pais(url)
                    return {
                        "producto_id": producto["id"], "producto": producto["nombre"],
                        "tienda": urlparse(url).netloc.lower().removeprefix("www."),
                        "tienda_nombre": tienda_nombre, "pais": pais, "es_internacional": es_inter,
                        "precio": precio_pw, "envio": extraer_envio(html_content), "url": url,
                        "titulo": titulo_real or producto["nombre"],
                        "condicion": detectar_condicion(url, titulo_real, html_content),
                    }
                else:
                    print(f"   ℹ️ Playwright cargó {urlparse(url).netloc} ({len(html_content)} bytes), pero no detectó precio.")
        else:
            print(f"   ⚠️ No se pudo extraer el precio en {urlparse(url).netloc} (Playwright no disponible en este entorno).")
        return None

async def buscar_producto_async(producto, pais_default, config, browser_mgr):
    consultas = generar_consultas(producto, config)
    resultados = []
    vistos = set()
    semaphore = asyncio.Semaphore(3) # Máximo 3 peticiones simultáneas por producto

    # 1. Procesar URLs fijas en paralelo
    urls_fijas = []
    if producto.get("url"): urls_fijas.append(producto["url"])
    if producto.get("urls") and isinstance(producto["urls"], list): urls_fijas.extend(producto["urls"])

    if urls_fijas:
        tasks = [consultar_url_async(u, producto, config, browser_mgr, semaphore) for u in urls_fijas]
        resultados_fijos = await asyncio.gather(*tasks, return_exceptions=True)
        for res in resultados_fijos:
            if isinstance(res, dict) and res:
                if es_resultado_valido(producto, res["titulo"], "", res["precio"], config, res["url"]):
                    print(f"   🎯 Oferta fija válida [{producto['nombre']}]: {res['precio']:.2f} € en {res['tienda_nombre']} -> {res['titulo'][:60]}")
                    resultados.append(res)
                    vistos.add(res["url"])
                else:
                    print(f"   🚫 Descartada URL fija [{producto['nombre']}]: '{res['titulo'][:50]}' ({res['precio']:.2f} € en {res['tienda_nombre']})")

    # 2. Búsqueda DuckDuckGo con FILTRADO ESTRICTO ANTES DE SCRAPEAR
    try:
        with DDGS() as buscador:
            for i, (consulta, region) in enumerate(consultas):
                if i > 0: await asyncio.sleep(random.uniform(0.5, 1.0))
                try:
                    encontrados = list(buscador.text(consulta, region=region, max_results=10))
                except Exception:
                    continue

                tasks_ddg = []
                for res in encontrados:
                    raw_url = res.get("href", "")
                    if not raw_url.startswith(("http://", "https://")): continue
                    
                    url = canonicalizar_url(raw_url)
                    domain = urlparse(url).netloc.lower().removeprefix("www.")
                    
                    # FILTRO 1: Dominios ignorados (buscadores, publicidad)
                    if any(ign in domain for ign in DOMINIOS_IGNORADOS): continue
                    
                    # FILTRO 2: Dominios que NO son tiendas (evita support, wikipedia, foros, vuelos, promociones, etc.)
                    dominios_no_tienda = [
                        "support.", "techcommunity.", "answers.", "wikipedia.org",
                        "youtube.com", "reddit.com", "twitter.com", "facebook.com",
                        "instagram.com", "tiktok.com", "vuelos.", "fluege.", "flights.",
                        "specials.", "newsletter."
                    ]
                    if any(no_tienda in domain for no_tienda in dominios_no_tienda): continue
                    
                    # FILTRO 3: URLs ya vistas
                    if url in vistos: continue
                    
                    # FILTRO 4: Validación estricta de URL de producto (usa tu función existente)
                    if not es_url_producto_valida(url, domain): continue
                    
                    # FILTRO 5: Solo permitir dominios de la UE si la opción está activa
                    if config.get("solo_union_europea", True):
                        if not es_tienda_ue(url): continue
                    elif not es_tienda_ue(url) and not any(x in domain for x in ["amazon.", "wallapop", "vinted", "ebay"]):
                        continue

                    tasks_ddg.append(consultar_url_async(url, producto, config, browser_mgr, semaphore))

                if tasks_ddg:
                    resultados_ddg = await asyncio.gather(*tasks_ddg, return_exceptions=True)
                    for res in resultados_ddg:
                        if isinstance(res, dict) and res and res["url"] not in vistos:
                            if es_resultado_valido(producto, res["titulo"], "", res["precio"], config, res["url"]):
                                print(f"   🎯 Oferta válida [{producto['nombre']}]: {res['precio']:.2f} € en {res['tienda_nombre']} -> {res['titulo'][:60]}")
                                resultados.append(res)
                                vistos.add(res["url"])
                            else:
                                print(f"   🚫 Descartado por filtro [{producto['nombre']}]: '{res['titulo'][:50]}' ({res['precio']:.2f} € en {res['tienda_nombre']})")
                                
    except Exception as error:
        print(f"Error general en buscador para '{producto['nombre']}': {error}")
        
    return resultados
    
# ============================================================
# PROGRAMA PRINCIPAL ACTUALIZADO (ASYNC)
# ============================================================
async def main_async():
    print("=" * 60)
    print("MONITOR UNIVERSAL DE PRECIOS (OPTIMIZADO ASYNC)")
    print("=" * 60)
    config = cargar_configuracion()
    estado = cargar_estado()
    productos_activos = [p for p in config.get("productos", []) if p.get("activo", True)]
    if not productos_activos:
        print("No hay productos activos configurados.")
        return
    print(f"Productos activos a monitorizar: {len(productos_activos)}")
    print("🌐 Iniciando motor de navegador (Playwright) para webs complejas...")
    browser_mgr = BrowserManager()
    await browser_mgr.start()
    try:
        tasks = [buscar_producto_async(p, config.get("pais", "España"), config, browser_mgr) for p in productos_activos]
        resultados_por_producto = await asyncio.gather(*tasks, return_exceptions=True)
        for producto, ofertas in zip(productos_activos, resultados_por_producto):
            if isinstance(ofertas, Exception):
                print(f"❌ Error al buscar '{producto.get('nombre')}': {ofertas}")
                continue
            print(f"\n🔎 {producto['nombre']}: {len(ofertas)} resultados válidos encontrados.")
            for oferta in ofertas:
                try:
                    procesar_oferta(producto, oferta, config, estado)
                except Exception as err_oferta:
                    print(f"   ⚠️ Error al procesar oferta: {err_oferta}")
    finally:
        print("\n💾 Guardando estado y cerrando navegador...")
        await browser_mgr.stop()
        guardar_estado(estado)
        print("✅ Monitor finalizado correctamente.")

if __name__ == "__main__":
    asyncio.run(main_async())