import csv
import hashlib
import json
import os
import re
import sys
import time
import random  # <-- ASEGÚRATE DE QUE ESTÉ
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3  # <-- AÑADIDO PARA REINTENTOS ROBUSTOS
from requests.adapters import HTTPAdapter
from ddgs import DDGS


# ============================================================
# RUTAS Y ARCHIVOS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "monitor_state.json"
HISTORY_FILE = BASE_DIR / "historico.csv"

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
    temporal = STATE_FILE.with_suffix(".tmp")
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False)
    temporal.replace(STATE_FILE)


# ============================================================
# EXTRACCION Y PARSEO DE NUMEROS, PRECIOS Y ENVIOS
# ============================================================

def convertir_numero(texto):
    texto = texto.strip().replace("\u00a0", "").replace(" ", "")
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".")
    return float(texto)


def extraer_precio(texto):
    # 1. ELIMINAR FALSOS POSITIVOS DE FILTROS LATERALES 
    # (ej: "menos de 25 €", "under 25 €", "unter 25 €")
    texto_limpio = re.sub(
        r'(?:under|moins de|sous|menos de|unter|less than|sotto|gifts under|regalos por debajo de|cadeaux de moins de)\s+\d+(?:[.,]\d+)?\s*(?:€|eur|euros?)', 
        '', 
        texto, 
        flags=re.IGNORECASE
    )
    
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


def es_tienda_ue(url):
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if domain.endswith(DOMINIOS_FUERA_UE) or any(d in domain for d in DOMINIOS_FUERA_UE):
        return False
    partes = domain.split(".")
    tld = partes[-1] if partes else ""
    if tld in PAISES_UE_TLD:
        return True
    tiendas_ue_conocidas = [
        "amazon.es", "amazon.de", "amazon.fr", "amazon.it", "amazon.nl", "amazon.com.be",
        "pccomponentes", "coolmod", "neobyte", "aussar", "senetic",
        "geizhals", "alternate", "caseking", "mindfactory", "ldlc", "mediamarkt",
        "wallapop", "vinted"
    ]
    if any(t in domain for t in tiendas_ue_conocidas):
        return True
    return False


def detectar_tienda_y_pais(url):
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    if "wallapop" in domain:
        return "Wallapop", "🇪🇸 Wallapop (2ª mano)", False
    if "vinted" in domain:
        return "Vinted", "🇪🇺 Vinted (2ª mano)", True
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
    if domain.endswith((".co.uk", ".uk")) or "amazon.co.uk" in domain:
        return domain, "🇬🇧 Reino Unido (Fuera UE)", True
    if domain.endswith(".es") or any(t in domain for t in ["pccomponentes", "coolmod", "neobyte", "aussar", "senetic"]):
        return domain, "🇪🇸 España", False
    if "geizhals" in domain:
        return "Geizhals (Europa)", "🇪🇺 Europa (UE)", True
    if "idealo" in domain:
        if domain.endswith(".de"): return "Idealo Alemania", "🇩🇪 Alemania", True
        if domain.endswith(".fr"): return "Idealo Francia", "🇫🇷 Francia", True
        if domain.endswith(".it"): return "Idealo Italia", "🇮🇹 Italia", True
        return "Idealo", "🇪🇺 Europa (UE)", True
    if "alternate" in domain:
        if domain.endswith(".de"): return "Alternate Alemania", "🇩🇪 Alemania", True
        return "Alternate", "🇪🇸 España", False
    if "caseking" in domain:
        return "Caseking", "🇩🇪 Alemania", True
    if "ldlc" in domain:
        return "LDLC", "🇫🇷 Francia", True
    if domain.endswith(".de"): return domain, "🇩🇪 Alemania", True
    if domain.endswith(".fr"): return domain, "🇫🇷 Francia", True
    if domain.endswith(".it"): return domain, "🇮🇹 Italia", True
    if domain.endswith(".nl"): return domain, "🇳🇱 Países Bajos", True
    if domain.endswith(".at"): return domain, "🇦🇹 Austria", True
    if domain.endswith(".pt"): return domain, "🇵🇹 Portugal", True
    return domain, "🇪🇺 Europa (UE)", True


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
    requeridos = producto.get("terminos_requeridos")
    if requeridos:
        for req in requeridos:
            if req.lower() not in texto:
                return False
    else:
        tokens = producto.get("busqueda", "").split()
        for token in tokens:
            # CAMBIO: Exigimos que CUALQUIER token de más de 2 caracteres esté en el texto.
            # Antes se ignoraba "64" porque no tenía letras. Ahora se exige su presencia.
            if len(token) >= 3 and token.lower() not in texto:
                return False

    # 4. Comprobar precio mínimo razonable
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

def obtener_credenciales_telegram():
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
    return (token.strip() if token else None), (str(chat_id).strip() if chat_id else None)


def enviar_telegram(mensaje):
    token, chat_id = obtener_credenciales_telegram()
    if not token or not chat_id:
        faltantes = []
        if not token: faltantes.append("TELEGRAM_BOT_TOKEN (o BOT_TOKEN en Secrets)")
        if not chat_id: faltantes.append("TELEGRAM_CHAT_ID (o CHAT_ID en Variables/Secrets)")
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

    mensaje = (
        f"{titulo}\n\n"
        f"📦 {producto['nombre']}\n"
        f"🏪 Tienda: {tienda} ({pais})\n\n"
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
        if enviar_telegram(mensaje):
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
    modelos = [
        t for t in tokens
        if re.search(r"[A-Za-z]", t) and re.search(r"\d", t) and len(t) >= 5
    ]

    if modelos:
        modelo_completo = modelos[0]
        # Clave: Separamos el modelo por el guion. Ej: "WD69EFPX-SPC5ZN0" -> "WD69EFPX"
        modelo_base = modelo_completo.split("-")[0] 

        # PARA AMAZON: Sin comillas y usando el modelo base (más flexible, evita que DDG no encuentre nada)
        consultas.append((f"{modelo_base} amazon", "es-es"))
        consultas.append((f"{modelo_completo} pccomponentes", "es-es")) # PcComponentes sí suele tener el modelo completo
        consultas.append((f"{modelo_base} precio", "es-es"))

        if buscar_europa:
            consultas.append((f"{modelo_base} amazon.de", "de-de"))
            consultas.append((f"{modelo_base} amazon.fr", "fr-fr"))
            consultas.append((f"{modelo_base} amazon.it", "it-it"))
            consultas.append((f"{modelo_base} geizhals.eu OR geizhals.de", "de-de"))
            consultas.append((f"{modelo_base} idealo.de OR idealo.es OR idealo.fr", "de-de"))
            consultas.append((f"{modelo_base} envio españa OR versand spanien", "es-es"))

        # Marketplaces C2C: Aquí SÍ usamos comillas y el modelo completo, y añadimos los filtros de "vendido"
        if config.get("incluir_wallapop_vinted", True):
            consultas.append((f'"{modelo_completo}" (site:wallapop.com OR site:es.wallapop.com) -vendido -reservado -sold -site:wallapop.co.uk', "es-es"))
            consultas.append((f'"{modelo_completo}" (site:vinted.es OR site:vinted.fr OR site:vinted.it OR site:vinted.de) -vendido -reservado -sold -site:vinted.co.uk', "es-es"))
            consultas.append((f'"{modelo_completo}" wallapop -vendido -reservado -uk -co.uk', "es-es"))
            consultas.append((f'"{modelo_completo}" vinted -vendido -reservado -uk -co.uk', "es-es"))

        consultas.append((f'"{modelo_completo}"', "es-es"))
        if buscar_europa:
            consultas.append((f'"{modelo_completo}"', "de-de"))
    else:
        consultas.append((f"{busqueda} precio", "es-es"))
        consultas.append((f"{busqueda} amazon", "es-es"))
        if config.get("incluir_wallapop_vinted", True):
            consultas.append((f"{busqueda} wallapop -vendido -reservado -uk -co.uk", "es-es"))
            consultas.append((f"{busqueda} vinted -vendido -reservado -uk -co.uk", "es-es"))
        if buscar_europa:
            consultas.append((f"{busqueda} amazon.de", "de-de"))
            consultas.append((f"{busqueda} geizhals", "de-de"))
        consultas.append((busqueda, "es-es"))
        if buscar_europa:
            consultas.append((busqueda, "de-de"))

    vistos = set()
    unicas = []
    for c, reg in consultas:
        if c not in vistos:
            vistos.add(c)
            unicas.append((c, reg))
    return unicas

def buscar_en_ddgs(buscador, consulta, region):
    for backend in ["auto", "lite", "html"]:
        try:
            res = buscador.text(consulta, region=region, backend=backend, max_results=15)
            if res:
                lista = list(res)
                if lista:
                    return lista
        except Exception:
            continue
    return []


def consultar_url_directa(url, producto, config):
    session = get_robust_session()
    
    # Copiamos los headers base y asignamos un User-Agent aleatorio para esta petición
    headers = BASE_HEADERS.copy()
    headers["User-Agent"] = random.choice(USER_AGENTS)
    
    try:
        # timeout=(5.0, 15.0) significa 5s para conectar, 15s para leer la respuesta
        respuesta = session.get(url, headers=headers, timeout=(5.0, 15.0))
        
        if respuesta.status_code != 200:
            return None
            
        texto = respuesta.text
        
        # ============================================================
        # NUEVO: FILTRO DE DISPONIBILIDAD PARA WALLAPOP Y VINTED
        # ============================================================
        if "wallapop" in url.lower() or "vinted" in url.lower():
            # 1. Comprobar el título de la página (muy fiable, cambia al venderse)
            titulo_pagina = re.search(r'<title[^>]*>(.*?)</title>', texto, re.IGNORECASE | re.DOTALL)
            if titulo_pagina:
                titulo = titulo_pagina.group(1).lower()
                palabras_no_disponible = ["vendido", "reservado", "sold", "vendu", "verkauft", "no disponible", "non disponible"]
                if any(palabra in titulo for palabra in palabras_no_disponible):
                    print(f"   ⏭️ Descartado (Vendido/Reservado en título): {url}")
                    return None
            
            # 2. Comprobar si el estado en la página indica claramente que está vendido
            # (Busca patrones como "Estado: Vendido" para evitar falsos positivos en anuncios laterales)
            if re.search(r'(?:estado|statut|status).*?:.*?(?:vendido|sold|verkauft|vendu|reservado|réservé)', texto, re.IGNORECASE):
                print(f"   ⏭️ Descartado (Estado marcado como vendido/reservado): {url}")
                return None
        # ============================================================

        precio = extraer_precio(texto)
        if not precio:
            return None
            
        tienda_nombre, pais, es_inter = detectar_tienda_y_pais(url)
        envio = extraer_envio(texto)
        
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
            "titulo": producto["nombre"],
        }
    except requests.exceptions.ConnectionError as e:
        # Si tras 3 reintentos sigue fallando (ej. IP de GitHub bloqueada), lo registramos
        print(f"   ⚠️ Conexión rechazada tras varios intentos en {urlparse(url).netloc}: {e}")
        return None
    except Exception as e:
        print(f"   ⚠️ Error inesperado en {url}: {e}")
        return None

def buscar_producto(producto, pais_default, config):
    consultas = generar_consultas(producto, config)
    resultados = []
    vistos = set()

    # 1. URLs fijas configuradas en el JSON
    urls_fijas = []
    if producto.get("url"):
        urls_fijas.append(producto["url"])
    if producto.get("urls") and isinstance(producto["urls"], list):
        urls_fijas.extend(producto["urls"])

    for u in urls_fijas:
        directa = consultar_url_directa(u, producto, config)
        if directa:
            vistos.add(directa["url"])
            resultados.append(directa)
        time.sleep(random.uniform(1.0, 2.5))

    # 2. Búsqueda con DuckDuckGo (para PcComponentes, Wallapop, Vinted, Idealo, etc.)
    try:
        with DDGS() as buscador:
            for consulta, region in consultas:
                encontrados = buscar_en_ddgs(buscador, consulta, region)
                for res in encontrados:
                    url = res.get("href", "")
                    if not url.startswith(("http://", "https://")) or url in vistos:
                        continue

                    # 🛡️ CORRECCIÓN CLAVE: Ignorar páginas de búsqueda de Amazon (/s?).
                    # Estas páginas devuelven 503 desde GitHub Actions. 
                    # Solo permitimos pasar a páginas de producto reales (que contienen /dp/)
                    if "amazon." in url.lower() and "/s?" in url.lower():
                        continue

                    titulo = res.get("title", "")
                    descripcion = res.get("body", "")
                    texto = f"{titulo} {descripcion}"

                    # A. Intentamos extraer precio del resumen de DuckDuckGo
                    precio = extraer_precio(texto)

                    # B. LÓGICA DE RESPALDO: Si no hay precio, pero es tienda fiable, visitamos la URL
                    if precio is None:
                        tiendas_fiables = [
                            "amazon.es", "amazon.de", "amazon.fr", "amazon.it", "amazon.nl",
                            "pccomponentes", "coolmod", "neobyte", "aussar", "senetic",
                            "geizhals", "idealo", "wallapop", "vinted"
                        ]
                        es_tienda_fiable = any(tienda in url.lower() for tienda in tiendas_fiables)
                        
                        termino_busqueda = producto.get("busqueda", "").lower()
                        palabras_clave = termino_busqueda.split()[:2] 
                        coincide_modelo = all(palabra in titulo.lower() for palabra in palabras_clave)

                        if es_tienda_fiable and coincide_modelo:
                            print(f"   🔍 Precio no visible en resumen DDG, visitando URL directa: {url}")
                            directa = consultar_url_directa(url, producto, config)
                            if directa:
                                resultados.append(directa)
                                vistos.add(url)
                        
                        continue # Pasamos al siguiente resultado de DDG

                    # C. Si SÍ había precio en el resumen, seguimos con el flujo normal de validación
                    if not es_resultado_valido(producto, titulo, descripcion, precio, config, url):
                        continue

                    vistos.add(url)
                    tienda_nombre, pais, es_inter = detectar_tienda_y_pais(url)
                    domain = urlparse(url).netloc.lower().removeprefix("www.")
                    envio = extraer_envio(texto)

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
                    })
                
                # Pausa humana entre cada consulta de búsqueda
                time.sleep(random.uniform(2.5, 5.0))
                
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

    token, chat_id = obtener_credenciales_telegram()
    if token:
        preview = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else "***"
        print(f"🤖 Bot de Telegram: Conectado (Token: {preview})")
    else:
        print("⚠️ Bot de Telegram: Falta Token (revisa Secret 'TELEGRAM_BOT_TOKEN' o 'BOT_TOKEN')")

    if chat_id:
        print(f"💬 Chat de Telegram: Conectado (ID: {chat_id})")
    else:
        print("⚠️ Chat de Telegram: Falta Chat ID (revisa Variable 'TELEGRAM_CHAT_ID' o 'CHAT_ID')")

    for producto in productos_activos:
        print(f"\n🔎 Buscando: {producto['nombre']} (Modelo: {producto.get('busqueda')})")
        ofertas = buscar_producto(producto, config.get("pais", "España"), config)
        print(f"   Resultados válidos encontrados: {len(ofertas)}")

        for oferta in ofertas:
            procesar_oferta(producto, oferta, config, estado)

        # time.sleep(1.5)
        time.sleep(random.uniform(3.0, 6.0)) # Pausa más larga entre productos diferentes

    # Guardamos el estado solo para recordar qué ofertas ya fueron notificadas
    guardar_estado(estado)

    print("\nMonitor finalizado correctamente.")


if __name__ == "__main__":
    main()