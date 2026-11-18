import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from ddgs import DDGS


# ============================================================
# CONFIGURACION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "monitor_state.json"
HISTORY_FILE = BASE_DIR / "historico.csv"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

COLUMNAS_CSV = [
    "fecha",
    "producto_id",
    "producto",
    "tienda",
    "precio",
    "envio",
    "total_unidad",
    "cantidad",
    "total_pedido",
    "precio_unidad_medida",
    "url",
]


# ============================================================
# CONFIGURACION Y ESTADO
# ============================================================

def cargar_configuracion():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    config.setdefault("intervalo_minutos", 60)
    config.setdefault("pais", "España")
    config.setdefault("moneda", "EUR")
    config.setdefault("incluir_envio", True)
    config.setdefault("notificar_bajadas", True)
    config.setdefault("solo_nuevas_ofertas", True)
    config.setdefault("productos", [])

    return config


def cargar_estado():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
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
# PRECIOS Y ENVIO
# ============================================================

def convertir_numero(texto):
    texto = texto.strip().replace(" ", "")

    # Formatos habituales: 1.299,90 o 1299,90 o 1299.90
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
        r"(\d+(?:[.,]\d{1,2})?)\s*EUR",
    ]

    for patron in patrones:
        for coincidencia in re.finditer(
            patron, texto, re.IGNORECASE
        ):
            try:
                precio = convertir_numero(coincidencia.group(1))

                if 1 <= precio <= 100000:
                    return precio
            except ValueError:
                continue

    return None


def extraer_envio(texto):
    texto_lower = texto.lower()

    if (
        "envío gratis" in texto_lower
        or "envio gratis" in texto_lower
        or "free shipping" in texto_lower
    ):
        return 0.0

    patrones = [
        r"(?:env[ií]o|portes|gastos de env[ií]o)"
        r"\s*(?:de|:)?\s*(\d+(?:[.,]\d{1,2})?)\s*€"
    ]

    for patron in patrones:
        coincidencia = re.search(patron, texto_lower)

        if coincidencia:
            try:
                return convertir_numero(coincidencia.group(1))
            except ValueError:
                pass

    return None


def obtener_tienda(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return "Tienda desconocida"


# ============================================================
# BUSQUEDA
# ============================================================

def buscar_producto(producto, pais):
    consulta = f"{producto['busqueda']} comprar precio {pais}"

    resultados = []
    vistos = set()

    try:
        with DDGS() as buscador:
            encontrados = buscador.text(
                consulta,
                max_results=20
            )

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

                envio = extraer_envio(texto)

                # No se inventan gastos de envío.
                if envio is None:
                    total_unidad = precio
                else:
                    total_unidad = precio + envio

                resultados.append({
                    "producto_id": producto["id"],
                    "producto": producto["nombre"],
                    "tienda": obtener_tienda(url),
                    "precio": precio,
                    "envio": envio,
                    "total_unidad": total_unidad,
                    "url": url,
                    "titulo": titulo,
                })

    except Exception as error:
        print(f"Error buscando {producto['nombre']}: {error}")

    return resultados


# ============================================================
# CALCULO DE PRECIO POR UNIDAD DE MEDIDA
# ============================================================

def calcular_precio_medida(producto, oferta):
    limite = producto.get("precio_maximo_por_unidad_medida")

    if limite is None:
        return None

    medida = producto.get("unidad_medida")

    if not medida:
        return None

    capacidad = producto.get("capacidad_por_unidad")

    if capacidad is None:
        capacidad = extraer_capacidad(
            producto["busqueda"] + " " + producto["nombre"],
            medida
        )

    if not capacidad or capacidad <= 0:
        return None

    return oferta["total_unidad"] / float(capacidad)


def extraer_capacidad(texto, unidad):
    """
    Extrae capacidades sencillas como 8 TB, 256 GB o 2 kg.
    Devuelve la cantidad expresada en la unidad configurada.
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
        valor = convertir_numero(coincidencia.group(1))

        # Conversiones a la unidad solicitada.
        if unidad == "tb":
            return valor
        if unidad == "gb":
            return valor
        if unidad == "kg":
            return valor
        if unidad == "g":
            return valor
        if unidad == "l":
            return valor
        if unidad == "ml":
            return valor

        return valor

    except ValueError:
        return None


# ============================================================
# OBJETIVOS DE PRECIO
# ============================================================

def evaluar_oferta(producto, oferta, config):
    cantidad = int(producto.get("cantidad", 1))

    total_pedido = oferta["total_unidad"] * cantidad

    precio_medida = calcular_precio_medida(
        producto, oferta
    )

    criterios = []

    max_unidad = producto.get("precio_maximo_unidad")

    if max_unidad is not None:
        criterios.append(
            oferta["total_unidad"] <= float(max_unidad)
        )

    max_pedido = producto.get("precio_maximo_pedido")

    if max_pedido is not None:
        criterios.append(
            total_pedido <= float(max_pedido)
        )

    max_medida = producto.get(
        "precio_maximo_por_unidad_medida"
    )

    if max_medida is not None:
        if precio_medida is not None:
            criterios.append(
                precio_medida <= float(max_medida)
            )

    # Se avisa si cumple al menos un criterio válido.
    cumple = any(criterios)

    # No se considera verificado el coste final si se
    # desconoce el envío.
    envio_conocido = oferta["envio"] is not None

    return {
        "cumple": cumple,
        "cantidad": cantidad,
        "total_pedido": total_pedido,
        "precio_medida": precio_medida,
        "envio_conocido": envio_conocido,
    }


# ============================================================
# HISTORICO
# ============================================================

def guardar_historico(producto, oferta, evaluacion):
    existe = HISTORY_FILE.exists()

    with open(
        HISTORY_FILE,
        "a",
        newline="",
        encoding="utf-8"
    ) as f:

        escritor = csv.DictWriter(
            f,
            fieldnames=COLUMNAS_CSV
        )

        if not existe or HISTORY_FILE.stat().st_size == 0:
            escritor.writeheader()

        escritor.writerow({
            "fecha": datetime.now(timezone.utc).isoformat(),
            "producto_id": producto["id"],
            "producto": producto["nombre"],
            "tienda": oferta["tienda"],
            "precio": oferta["precio"],
            "envio": (
                oferta["envio"]
                if oferta["envio"] is not None
                else "Desconocido"
            ),
            "total_unidad": oferta["total_unidad"],
            "cantidad": evaluacion["cantidad"],
            "total_pedido": evaluacion["total_pedido"],
            "precio_unidad_medida": (
                evaluacion["precio_medida"]
                if evaluacion["precio_medida"] is not None
                else "No disponible"
            ),
            "url": oferta["url"],
        })


# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Faltan los secretos de Telegram.")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:
        respuesta = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": mensaje,
                "disable_web_page_preview": False,
            },
            timeout=20,
        )

        respuesta.raise_for_status()

        datos = respuesta.json()

        if not datos.get("ok"):
            print(f"Telegram devolvió un error: {datos}")
            return False

        return True

    except requests.RequestException as error:
        print(f"Error enviando Telegram: {error}")
        return False


def crear_mensaje(producto, oferta, evaluacion, bajada=False):
    moneda = "€"

    envio_texto = (
        f"{oferta['envio']:.2f} {moneda}"
        if oferta["envio"] is not None
        else "Pendiente de confirmar"
    )

    titulo = (
        "📉 BAJADA DE PRECIO"
        if bajada
        else "🛒 NUEVA OFERTA"
    )

    mensaje = (
        f"{titulo}\n\n"
        f"📦 {producto['nombre']}\n"
        f"🏪 Tienda: {oferta['tienda']}\n\n"
        f"💶 Precio unidad: {oferta['precio']:.2f} {moneda}\n"
        f"🚚 Envío: {envio_texto}\n"
        f"💰 Total unidad calculado: "
        f"{oferta['total_unidad']:.2f} {moneda}\n"
        f"🔢 Cantidad: {evaluacion['cantidad']}\n"
        f"🧾 Total pedido calculado: "
        f"{evaluacion['total_pedido']:.2f} {moneda}\n"
    )

    if evaluacion["precio_medida"] is not None:
        unidad = producto.get("unidad_medida", "")
        mensaje += (
            f"📊 Precio por {unidad}: "
            f"{evaluacion['precio_medida']:.2f} {moneda}\n"
        )

    if not evaluacion["envio_conocido"]:
        mensaje += (
            "\n⚠️ Gastos de envío no confirmados. "
            "El total es provisional.\n"
        )

    mensaje += (
        "\n🔗 ENLACE A LA OFERTA:\n"
        f"{oferta['url']}"
    )

    return mensaje


# ============================================================
# CONTROL DE DUPLICADOS Y BAJADAS
# ============================================================

def clave_oferta(producto, oferta):
    base = (
        f"{producto['id']}|"
        f"{oferta['url']}"
    )

    return hashlib.sha256(
        base.encode("utf-8")
    ).hexdigest()


def procesar_oferta(producto, oferta, config, estado):
    evaluacion = evaluar_oferta(
        producto,
        oferta,
        config
    )

    guardar_historico(
        producto,
        oferta,
        evaluacion
    )

    clave = clave_oferta(producto, oferta)

    ofertas_guardadas = estado.setdefault("ofertas", {})
    anterior = ofertas_guardadas.get(clave)

    precio_actual = oferta["total_unidad"]

    es_nueva = anterior is None

    ha_bajado = (
        anterior is not None
        and precio_actual < float(anterior["precio"])
    )

    avisar = False
    es_bajada = False

    if evaluacion["cumple"]:

        if es_nueva:
            avisar = True

        elif ha_bajado and config["notificar_bajadas"]:
            avisar = True
            es_bajada = True

        elif not config["solo_nuevas_ofertas"] and not ha_bajado:
            avisar = True

    if avisar:
        mensaje = crear_mensaje(
            producto,
            oferta,
            evaluacion,
            es_bajada
        )

        if enviar_telegram(mensaje):
            ofertas_guardadas[clave] = {
                "precio": precio_actual,
                "ultima_alerta": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

    else:
        if anterior is None:
            ofertas_guardadas[clave] = {
                "precio": precio_actual,
                "ultima_alerta": None,
            }

        elif ha_bajado:
            ofertas_guardadas[clave]["precio"] = precio_actual


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print("=" * 50)
    print("MONITOR UNIVERSAL DE PRECIOS")
    print("=" * 50)

    config = cargar_configuracion()
    estado = cargar_estado()

    ahora = time.time()

    intervalo = (
        int(config["intervalo_minutos"]) * 60
    )

    ultima = float(
        estado.get("ultima_busqueda", 0)
    )

    if ahora - ultima < intervalo:
        faltan = int(
            (intervalo - (ahora - ultima)) / 60
        )

        print(
            f"Todavía no toca buscar. "
            f"Quedan aproximadamente {faltan} minutos."
        )
        return

    if not config["productos"]:
        print("No hay productos configurados.")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ATENCIÓN: faltan los secretos de Telegram.")

    productos_activos = [
        p for p in config["productos"]
        if p.get("activo", True)
    ]

    print(
        f"Productos activos: {len(productos_activos)}"
    )

    for producto in productos_activos:

        print(
            f"\nBuscando: {producto['nombre']}"
        )

        ofertas = buscar_producto(
            producto,
            config["pais"]
        )

        print(
            f"Resultados con precio: {len(ofertas)}"
        )

        for oferta in ofertas:
            print(
                f"{oferta['tienda']} - "
                f"{oferta['precio']:.2f} €"
            )

            procesar_oferta(
                producto,
                oferta,
                config,
                estado
            )

        # Pequeña pausa entre productos.
        time.sleep(2)

    estado["ultima_busqueda"] = time.time()

    guardar_estado(estado)

    print("\nMonitor finalizado correctamente.")


if __name__ == "__main__":
    main()
