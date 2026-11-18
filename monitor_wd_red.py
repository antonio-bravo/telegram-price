#!/usr/bin/env python3
"""
WD Red Plus price monitor.

Searches public search-engine snippets for exact WD Red Plus model numbers.
Important: snippets are indicative, not checkout-verified prices. Shipping is
only treated as known when the snippet explicitly states it.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ddgs import DDGS

ROOT = Path(__file__).resolve().parent
HISTORY_FILE = ROOT / "historico_wd_red.csv"
STATE_FILE = ROOT / "monitor_state.json"

MODELS = {
    "WD60EFPX": {"tb": 6},
    "WD80EFPX": {"tb": 8},
}
UNITS = 2
GOOD_EUR_PER_TB = float(os.getenv("GOOD_EUR_PER_TB", "42"))
TARGET_EUR_PER_TB = float(os.getenv("TARGET_EUR_PER_TB", "40"))
WATCH_EUR_PER_TB = float(os.getenv("WATCH_EUR_PER_TB", "45"))
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "10"))
REGION = os.getenv("DDGS_REGION", "es-es")
SEARCH_DELAY_SECONDS = float(os.getenv("SEARCH_DELAY_SECONDS", "1.5"))

QUERIES = [
    '"{model}" comprar precio',
    '"{model}" oferta tienda',
    '"{model}" España precio',
    '"{model}" 6TB OR 8TB disco NAS',
    '"{model}" gastos envío',
]

CSV_FIELDS = [
    "checked_at_utc", "model", "capacity_tb", "unit_price_eur",
    "shipping_order_eur", "shipping_status", "two_drive_total_eur",
    "eur_per_tb", "store_domain", "title", "url", "snippet",
]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_euro_amount(raw: str) -> float | None:
    """Parse common Spanish/European and English price formats."""
    value = raw.strip().replace("\u00a0", "").replace(" ", "")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        amount = float(value)
        return amount if 40 <= amount <= 3000 else None
    except ValueError:
        return None


def extract_price(text: str) -> float | None:
    patterns = [
        r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?)\s*€",
        r"€\s*(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?)",
        r"(\d{2,4}[,.]\d{2})\s*(?:EUR|euros?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            price = parse_euro_amount(match.group(1))
            if price is not None:
                return price
    return None


def extract_shipping(text: str) -> tuple[float | None, str]:
    lower = text.lower()
    free_terms = (
        "envío gratis", "envio gratis", "envío gratuito", "envio gratuito",
        "portes gratis", "free shipping", "free delivery",
    )
    if any(term in lower for term in free_terms):
        return 0.0, "free_explicit"

    patterns = [
        r"(?:gastos de )?env[ií]o\s*(?:desde|de|:)?\s*(\d{1,3}[,.]\d{2})\s*€",
        r"(?:shipping|delivery)\s*(?:from|:)?\s*€?\s*(\d{1,3}[,.]\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, lower, flags=re.IGNORECASE)
        if match:
            amount = parse_euro_amount(match.group(1))
            if amount is not None:
                return amount, "explicit_amount"
    return None, "unknown"


def valid_product_result(model: str, title: str, snippet: str) -> bool:
    combined = f"{title} {snippet}".lower()
    if model.lower() not in combined:
        return False
    exclusions = (
        "red pro", "recertified", "refurbished", "reacondicionado",
        "segunda mano", "usado", "used", "renewed",
    )
    return not any(term in combined for term in exclusions)


def search_offers() -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    with DDGS() as search:
        for model, spec in MODELS.items():
            for template in QUERIES:
                query = template.format(model=model)
                logging.info("Buscando: %s", query)
                try:
                    results = search.text(
                        query,
                        region=REGION,
                        max_results=MAX_RESULTS,
                    )
                    for item in results:
                        title = str(item.get("title") or "")
                        snippet = str(item.get("body") or "")
                        url = str(item.get("href") or "")
                        if not url.startswith(("http://", "https://")):
                            continue
                        if not valid_product_result(model, title, snippet):
                            continue

                        price = extract_price(f"{title}. {snippet}")
                        if price is None:
                            continue

                        shipping, shipping_status = extract_shipping(
                            f"{title}. {snippet}"
                        )
                        total = None
                        eur_per_tb = None
                        if shipping is not None:
                            total = UNITS * price + shipping
                            eur_per_tb = total / (UNITS * spec["tb"])

                        domain = urlparse(url).netloc.lower()
                        record = {
                            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                            "model": model,
                            "capacity_tb": spec["tb"],
                            "unit_price_eur": round(price, 2),
                            "shipping_order_eur": (
                                round(shipping, 2) if shipping is not None else ""
                            ),
                            "shipping_status": shipping_status,
                            "two_drive_total_eur": (
                                round(total, 2) if total is not None else ""
                            ),
                            "eur_per_tb": (
                                round(eur_per_tb, 2) if eur_per_tb is not None else ""
                            ),
                            "store_domain": domain,
                            "title": title[:300],
                            "url": url,
                            "snippet": snippet[:1000],
                        }
                        found[(model, url)] = record
                except Exception as exc:
                    logging.warning("Falló búsqueda %r: %s", query, exc)
                time.sleep(SEARCH_DELAY_SECONDS)
    return list(found.values())


def append_history(offers: list[dict[str, Any]]) -> None:
    exists = HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(offers)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"last_seen": {}, "sent_alerts": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("last_seen", {})
        data.setdefault("sent_alerts", [])
        return data
    except (json.JSONDecodeError, OSError):
        logging.warning("Estado inválido; se inicia uno nuevo.")
        return {"last_seen": {}, "sent_alerts": []}


def send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logging.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
        return False

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(endpoint, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok"):
            return True
        logging.error("Telegram API respondió con error: %s", result)
    except Exception as exc:
        logging.exception("No se pudo enviar Telegram: %s", exc)
    return False


def alert_candidates(
    offers: list[dict[str, Any]], state: dict[str, Any]
) -> None:
    last_seen = state["last_seen"]
    sent = set(state["sent_alerts"])

    for offer in offers:
        eur_per_tb = offer["eur_per_tb"]
        if eur_per_tb == "":
            continue  # Sin envío confirmado no se afirma coste final.
        eur_per_tb = float(eur_per_tb)
        if eur_per_tb > WATCH_EUR_PER_TB:
            continue

        model = offer["model"]
        url = offer["url"]
        price = float(offer["unit_price_eur"])
        shipping = float(offer["shipping_order_eur"])
        listing_key = f"{model}|{url}"
        current_key = f"{listing_key}|{price:.2f}|{shipping:.2f}"
        previous = last_seen.get(listing_key)

        # Alertar por primera aparición bajo 45 €/TB, o si baja el coste
        # total respecto a la observación anterior.
        is_new_qualifying = previous is None and current_key not in sent
        is_price_drop = (
            previous is not None
            and float(previous.get("eur_per_tb", 999999)) > eur_per_tb
        )
        if not is_new_qualifying and not is_price_drop:
            last_seen[listing_key] = {
                "unit_price_eur": price,
                "eur_per_tb": eur_per_tb,
                "seen_at": offer["checked_at_utc"],
            }
            continue

        if eur_per_tb <= TARGET_EUR_PER_TB:
            label = "🔥 OFERTA OBJETIVO (≤ 40 €/TB)"
        elif eur_per_tb <= GOOD_EUR_PER_TB:
            label = "🟢 MUY BUENA OFERTA (≤ 42 €/TB)"
        else:
            label = "🟡 OFERTA EN VIGILANCIA (≤ 45 €/TB)"

        drop_note = "\n📉 Ha bajado respecto a la última detección." if is_price_drop else ""
        message = (
            f"{label}{drop_note}\n\n"
            f"WD Red Plus {offer['capacity_tb']} TB — {model}\n"
            f"Tienda: {offer['store_domain']}\n"
            f"Precio por disco: {price:.2f} €\n"
            f"Envío detectado para el pedido: {shipping:.2f} €\n"
            f"Total estimado de 2 discos: "
            f"{float(offer['two_drive_total_eur']):.2f} €\n"
            f"Coste: {eur_per_tb:.2f} €/TB\n\n"
            f"{offer['title']}\n{url}\n\n"
            "⚠️ Precio y envío extraídos del resultado de búsqueda. "
            "Comprueba el importe final, disponibilidad, vendedor, "
            "estado nuevo y garantía antes de comprar."
        )

        if send_telegram(message):
            sent.add(current_key)

        last_seen[listing_key] = {
            "unit_price_eur": price,
            "eur_per_tb": eur_per_tb,
            "seen_at": offer["checked_at_utc"],
        }

    state["sent_alerts"] = sorted(sent)
    state["last_seen"] = last_seen
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    setup_logging()
    logging.info("Iniciando monitor WD Red Plus.")
    logging.info(
        "Umbrales: vigilancia %.2f, buena %.2f, objetivo %.2f €/TB",
        WATCH_EUR_PER_TB, GOOD_EUR_PER_TB, TARGET_EUR_PER_TB,
    )

    offers = search_offers()
    logging.info("Resultados con modelo y precio detectados: %d", len(offers))
    append_history(offers)

    state = load_state()
    alert_candidates(offers, state)

    # Resumen de ejecución para el log de GitHub Actions.
    for offer in sorted(
        offers,
        key=lambda x: float(x["eur_per_tb"]) if x["eur_per_tb"] != "" else 999999,
    )[:15]:
        shipping_text = (
            f"{offer['shipping_order_eur']} €"
            if offer["shipping_order_eur"] != ""
            else "no confirmado"
        )
        logging.info(
            "%s | %s | %s € | envío %s | %s",
            offer["model"], offer["store_domain"],
            offer["unit_price_eur"], shipping_text, offer["url"],
        )

    logging.info("Monitor finalizado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
