import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
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
# CARGA DE CONFIGURACION Y ESTADO
# ============================================================

def cargar_configuracion():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    config.setdefault("intervalo_minutos", 60)
    config.setdefault("pais", "España")
    config.setdefault("moneda", "EUR")
    config.setdefault("incluir_envio", True)
    config.setdefault("exigir_envio_confirmado", True)
    config.setdefault("envio_por_defecto", None)
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
                    datos.setdefault("ultima_busqueda", 0)
                    datos.setdefault("ofertas", {})
                    return datos
        except (OSError, json.JSONDecodeError):
            print("Estado anterior inválido. Se inicia uno nuevo.")

    return {
        "ultima_busqueda": 0,
        "ofertas": {}
    }


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
    patrones = [
        r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*€",
        r"€\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)",
        r"(\d+(?:[.,]\d{1,2})?)\s*(?:EUR|euros?)\b",
    ]

    for patron in patrones:
        for coincidencia in re.finditer(patron, texto, re.IGNORECASE):
            try:
                precio = convertir_numero(coincidencia.group(1))
                if 1 <= precio <= 100000:
                    return precio
            except ValueError:
                continue

    return None


def extraer_envio(texto):
    """
    Detecta si el snippet indica expresamente envío gratis o un coste en euros.
    Devuelve float (0.0 para gratis, >0 si tiene coste) o None si no se menciona.
    """
    texto_lower = texto.lower()

    terminos_gratis = [
        "envío gratis", "envio gratis",
        "envío gratuito", "envio gratuito",
        "portes gratis", "portes gratuitos",
        "entrega gratis", "entrega gratuita",
        "sin gastos de envío", "sin gastos de envio",
        "gastos de envío gratis", "gastos de envio gratis",
        "free shipping", "free delivery"
    ]

    for termino in terminos_gratis:
        if termino in texto_lower:
            return 0.0

    patrones = [
        r"(?:gastos de |coste de )?env[ií]o\s*(?:desde|de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"(?:gastos de |coste de )?env[ií]o\s*(?:desde|de|:)?\s*€\s*(\d+(?:[.,]\d{1,2})?)",
        r"portes\s*(?:desde|de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€",
        r"(?:shipping|delivery)\s*(?:from|:)?\s*€?\s*(\d+(?:[.,]\d{1,2})?)",
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
    """
    Extrae la capacidad de textos como 'WD80EFPX 8TB' o 'iPhone 256GB'.
    """
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


def obtener_tienda(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return "Tienda desconocida"


# ============================================================
# VALIDACION Y FILTRADO DE RESULTADOS
# ============================================================

def es_resultado_valido(producto, titulo, descripcion, precio, config):
    """
    Evita falsos positivos descartando accesorios, reacondicionados
    o resultados que no coincidan con el producto buscado.
    """
    texto = f"{titulo} {descripcion}".lower()

    # 1. Comprobar exclusiones (reacondicionados, cables, carcasas, etc.)
    exclusiones = (
        producto.get("excluir_terminos")
        if producto.get("excluir_terminos") is not None
        else config.get("excluir_terminos", TERMINOS_EXCLUSION_DEFECTO)
    )
    for termino in exclusiones:
        if termino.lower() in texto:
            return False

    # 2. Comprobar que contenga el modelo o términos clave obligatorios
    requeridos = producto.get("terminos_requeridos")
    if requeridos:
        for req in requeridos:
            if req.lower() not in texto:
                return False
    else:
        # Detectar automáticamente si la búsqueda tiene un modelo alfanumérico (ej: WD80EFPX)
        tokens = producto.get("busqueda", "").split()
        for token in tokens:
            if re.search(r"[A-Za-z]", token) and re.search(r"\d", token) and len(token) >= 5:
                if token.lower() not in texto:
                    return False

    # 3. Comprobar precio mínimo razonable para evitar cables/accesorios
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

def calcular_costes(producto, precio, envio, config):
    """
    Calcula el coste por unidad y del pedido completo.
    El envío se aplica una única vez al total del pedido.
    """
    cantidad = int(producto.get("cantidad", 1))
    incluir_envio = config.get("incluir_envio", True)
    envio_por_defecto = config.get("envio_por_defecto")

    envio_conocido = envio is not None
    coste_envio = 0.0

    if incluir_envio:
        if envio_conocido:
            coste_envio = float(envio)
        elif envio_por_defecto is not None:
            coste_envio = float(envio_por_defecto)

    # El envío de la tienda se abona una sola vez por pedido
    total_pedido = (precio * cantidad) + coste_envio
    total_unidad = total_pedido / cantidad

    return {
        "precio_base": precio,
        "envio": envio,
        "envio_conocido": envio_conocido,
        "coste_envio_aplicado": coste_envio,
        "cantidad": cantidad,
        "total_unidad": total_unidad,
        "total_pedido": total_pedido,
    }


def calcular_precio_medida(producto, total_unidad):
    """
    Calcula el coste por unidad de medida (ej. €/TB, €/kg).
    """
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
    """
    Evalúa si la oferta cumple TODOS los criterios definidos (no null).
    Cualquier criterio puesto a null es ignorado.
    Si se exige que el envío esté confirmado y no se conoce, se descarta.
    """
    incluir_envio = config.get("incluir_envio", True)
    exigir_confirmado = config.get("exigir_envio_confirmado", True)

    # Si se exige incluir envío y que esté confirmado, y no se conoce:
    if incluir_envio and exigir_confirmado and not costes["envio_conocido"] and config.get("envio_por_defecto") is None:
        return {
            "cumple": False,
            "motivo_descarte": "Gastos de envío desconocidos en el resultado (se exige envío confirmado)",
            "precio_medida": precio_medida,
            **costes,
        }

    criterios_definidos = 0
    criterios_cumplidos = 0

    # 1. Criterio: Precio máximo por unidad
    max_unidad = producto.get("precio_maximo_unidad")
    if max_unidad is not None:
        criterios_definidos += 1
        if costes["total_unidad"] <= float(max_unidad):
            criterios_cumplidos += 1

    # 2. Criterio: Precio máximo por pedido
    max_pedido = producto.get("precio_maximo_pedido")
    if max_pedido is not None:
        criterios_definidos += 1
        if costes["total_pedido"] <= float(max_pedido):
            criterios_cumplidos += 1

    # 3. Criterio: Precio máximo por unidad de medida (ej. €/TB)
    max_medida = producto.get("precio_maximo_por_unidad_medida")
    if max_medida is not None:
        criterios_definidos += 1
        if precio_medida is not None and precio_medida <= float(max_medida):
            criterios_cumplidos += 1

    # Debe cumplir TODOS los criterios activos (no null)
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
            "tienda": oferta["tienda"],
            "precio_producto": f"{oferta['precio']:.2f}",
            "envio": (
                f"{evaluacion['envio']:.2f}"
                if evaluacion["envio_conocido"]
                else "Desconocido"
            ),
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

def enviar_telegram(mensaje):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("  ⚠️ Falta configurar TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
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

    if evaluacion["envio_conocido"]:
        if evaluacion["envio"] == 0.0:
            envio_texto = "GRATIS (0.00 €)"
        else:
            envio_texto = f"{evaluacion['envio']:.2f} {moneda}"
    else:
        if evaluacion["coste_envio_aplicado"] > 0:
            envio_texto = f"{evaluacion['coste_envio_aplicado']:.2f} {moneda} (Estimado)"
        else:
            envio_texto = "Desconocido (no indicado en web)"

    mensaje = (
        f"{titulo}\n\n"
        f"📦 {producto['nombre']}\n"
        f"🏪 Tienda: {oferta['tienda']}\n\n"
        f"💶 Precio producto: {oferta['precio']:.2f} {moneda}\n"
        f"🚚 Gastos de envío: {envio_texto}\n"
        f"💰 Total unidad (con envío): {evaluacion['total_unidad']:.2f} {moneda}\n"
        f"🔢 Cantidad pedido: {evaluacion['cantidad']}\n"
        f"🧾 Total pedido (con envío): {evaluacion['total_pedido']:.2f} {moneda}\n"
    )

    if evaluacion["precio_medida"] is not None:
        unidad = producto.get("unidad_medida", "")
        mensaje += (
            f"📊 Coste por {unidad}: {evaluacion['precio_medida']:.2f} {moneda}/{unidad}\n"
        )

    if not evaluacion["envio_conocido"]:
        mensaje += "\n⚠️ Gastos de envío no confirmados en la web. Comprueba antes de comprar.\n"

    mensaje += f"\n🔗 ENLACE A LA OFERTA:\n{oferta['url']}"
    return mensaje


# ============================================================
# PROCESAMIENTO Y CONTROL DE OFERTAS
# ============================================================

def clave_oferta(producto, oferta):
    base = f"{producto['id']}|{oferta['url']}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def procesar_oferta(producto, oferta, config, estado):
    costes = calcular_costes(producto, oferta["precio"], oferta["envio"], config)
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
        print(f"  🔔 ¡Oferta cumple criterios! Enviando a Telegram: {evaluacion['total_unidad']:.2f} €/unidad...")
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
            print(f"  ℹ️ {oferta['tienda']} ({oferta['precio']:.2f} €): {evaluacion['motivo_descarte']}")
        if anterior is None:
            ofertas_guardadas[clave] = {
                "precio": precio_actual,
                "ultima_alerta": None,
            }
        elif ha_bajado:
            ofertas_guardadas[clave]["precio"] = precio_actual


# ============================================================
# BUSQUEDA MEDIANTE DDGS
# ============================================================

def buscar_producto(producto, pais, config):
    consulta = f"{producto['busqueda']} comprar precio {pais}"
    resultados = []
    vistos = set()

    try:
        with DDGS() as buscador:
            encontrados = buscador.text(consulta, max_results=25)

            for resultado in encontrados:
                titulo = resultado.get("title", "")
                descripcion = resultado.get("body", "")
                url = resultado.get("href", "")

                if not url.startswith(("http://", "https://")):
                    continue

                if url in vistos:
                    continue
                vistos.add(url)

                texto = f"{titulo} {descripcion}"
                precio = extraer_precio(texto)
                if precio is None:
                    continue

                if not es_resultado_valido(producto, titulo, descripcion, precio, config):
                    continue

                envio = extraer_envio(texto)

                resultados.append({
                    "producto_id": producto["id"],
                    "producto": producto["nombre"],
                    "tienda": obtener_tienda(url),
                    "precio": precio,
                    "envio": envio,
                    "url": url,
                    "titulo": titulo,
                })

    except Exception as error:
        print(f"Error buscando '{producto['nombre']}': {error}")

    return resultados


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print("=" * 60)
    print("MONITOR UNIVERSAL DE PRECIOS")
    print("=" * 60)

    config = cargar_configuracion()
    estado = cargar_estado()

    ahora = time.time()
    intervalo = int(config.get("intervalo_minutos", 60)) * 60
    ultima = float(estado.get("ultima_busqueda", 0))

    forzar = (
        os.getenv("GITHUB_ACTIONS") == "true"
        or os.getenv("FORZAR_BUSQUEDA") == "1"
        or "--force" in sys.argv
    )

    if not forzar and (ahora - ultima < intervalo):
        faltan = int((intervalo - (ahora - ultima)) / 60)
        print(f"Todavía no toca buscar. Quedan aproximadamente {faltan} minutos.")
        print("Para forzar la búsqueda usa: python monitor.py --force")
        return

    productos_activos = [p for p in config.get("productos", []) if p.get("activo", True)]
    if not productos_activos:
        print("No hay productos activos configurados.")
        return

    print(f"Productos activos a monitorizar: {len(productos_activos)}")

    for producto in productos_activos:
        print(f"\n🔎 Buscando: {producto['nombre']} ({producto.get('busqueda')})")
        ofertas = buscar_producto(producto, config.get("pais", "España"), config)
        print(f"   Resultados válidos encontrados: {len(ofertas)}")

        for oferta in ofertas:
            procesar_oferta(producto, oferta, config, estado)

        time.sleep(2)

    estado["ultima_busqueda"] = time.time()
    guardar_estado(estado)

    print("\nMonitor finalizado correctamente.")


if __name__ == "__main__":
    main()
