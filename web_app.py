#!/usr/bin/env python3
"""
Servidor Web Ultraligero para Monitor de Precios.
Permite gestionar productos en config.json, previsualizar historico.csv y monitor_state.json,
y ejecutar comprobaciones bajo demanda desde cualquier dispositivo de la red local.
No requiere librerías externas (solo Python standard library).
"""

import os
import sys
import json
import csv
import base64
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import github_sync
except ImportError:
    github_sync = None

# ============================================================
# AUTENTICACIÓN BÁSICA OPCIONAL
# ============================================================
AUTH_USER = os.getenv("AUTH_USER", os.getenv("WEB_USER", "")).strip()
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", os.getenv("WEB_PASSWORD", "")).strip()
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)


# ============================================================
# RESOLUCIÓN DE RUTAS (COINCIDENTE CON MONITOR.PY)
# ============================================================

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data" if Path("/app/data").exists() else "."))

def resolver_ruta(env_var, nombre_archivo):
    env_val = os.getenv(env_var)
    if env_val and Path(env_val).exists():
        return Path(env_val)
    if (DATA_DIR / nombre_archivo).exists():
        return DATA_DIR / nombre_archivo
    if Path(f"/app/{nombre_archivo}").exists():
        return Path(f"/app/{nombre_archivo}")
    return Path(nombre_archivo)

CONFIG_FILE = resolver_ruta("CONFIG_PATH", "config.json")
STATE_FILE = resolver_ruta("STATE_PATH", "monitor_state.json")
HISTORY_FILE = resolver_ruta("HISTORY_PATH", "historico.csv")

# Estado de ejecuciones bajo demanda
EJECUCION_LOCK = threading.Lock()
ESTADO_EJECUCION = {
    "en_curso": False,
    "ultimo_inicio": None,
    "ultimo_fin": None,
    "ultimo_codigo": None,
    "logs": []
}

def ejecutar_monitor_hilo():
    global ESTADO_EJECUCION
    with EJECUCION_LOCK:
        ESTADO_EJECUCION["en_curso"] = True
        ESTADO_EJECUCION["ultimo_inicio"] = datetime.now(timezone.utc).isoformat()
        ESTADO_EJECUCION["logs"] = [f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando comprobación manual del monitor..."]

    try:
        cmd = [sys.executable, "monitor.py"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        for line in proc.stdout:
            clean_line = line.rstrip()
            if clean_line:
                with EJECUCION_LOCK:
                    ESTADO_EJECUCION["logs"].append(clean_line)
                    if len(ESTADO_EJECUCION["logs"]) > 200:
                        ESTADO_EJECUCION["logs"].pop(0)

        proc.wait()
        with EJECUCION_LOCK:
            ESTADO_EJECUCION["ultimo_codigo"] = proc.returncode
            ESTADO_EJECUCION["logs"].append(
                f"[{datetime.now().strftime('%H:%M:%S')}] Proceso finalizado con código: {proc.returncode}"
            )
    except Exception as e:
        with EJECUCION_LOCK:
            ESTADO_EJECUCION["logs"].append(f"⚠️ Error al ejecutar monitor: {e}")
    finally:
        with EJECUCION_LOCK:
            ESTADO_EJECUCION["en_curso"] = False
            ESTADO_EJECUCION["ultimo_fin"] = datetime.now(timezone.utc).isoformat()


# ============================================================
# LECTURA Y ESCRITURA SEGURA DE ARCHIVOS
# ============================================================

def leer_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return {"error": f"Error al leer config.json: {e}", "productos": []}
    return {"productos": []}

def guardar_config(datos):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        try:
            temp.replace(CONFIG_FILE)
        except OSError:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(datos, f, indent=2, ensure_ascii=False)
            if temp.exists():
                temp.unlink()
        if github_sync and github_sync.esta_activo():
            threading.Thread(
                target=github_sync.subir_archivo_github,
                args=("config.json", CONFIG_FILE, "Web: Actualizar config.json [skip ci]"),
                daemon=True
            ).start()
        return True, "Configuración guardada correctamente"
    except Exception as e:
        return False, str(e)

def leer_historial():
    if not HISTORY_FILE.exists():
        return []
    filas = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            lector = csv.DictReader(f)
            for r in lector:
                filas.append(r)
    except Exception as e:
        print(f"Error leyendo historico.csv: {e}")
    # Retornar las más recientes primero
    filas.reverse()
    return filas

def leer_estado():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"ofertas": {}}

def guardar_estado(datos):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        try:
            temp.replace(STATE_FILE)
        except OSError:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(datos, f, indent=2, ensure_ascii=False)
            if temp.exists():
                temp.unlink()
        if github_sync and github_sync.esta_activo():
            threading.Thread(
                target=github_sync.subir_archivo_github,
                args=("monitor_state.json", STATE_FILE, "Web: Actualizar monitor_state.json [skip ci]"),
                daemon=True
            ).start()
        return True, "Estado guardado"
    except Exception as e:
        return False, str(e)


# ============================================================
# PLANTILLA HTML/CSS/JS (EMBEBIDA, RESPONSIVE, ZERO DEPENDENCIAS)
# ============================================================

HTML_PAGE = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Monitor de Precios - Panel de Control</title>
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --text: #c9d1d9;
      --text-muted: #8b949e;
      --accent: #238636;
      --accent-hover: #2ea043;
      --blue: #58a6ff;
      --danger: #da3633;
      --warning: #d29922;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--font);
      line-height: 1.5;
      padding-bottom: 60px;
    }
    header {
      background-color: var(--card-bg);
      border-bottom: 1px solid var(--border);
      padding: 12px 20px;
      position: sticky;
      top: 0;
      z-index: 100;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
    }
    .logo-area { display: flex; align-items: center; gap: 10px; }
    .logo-area h1 { font-size: 1.15rem; font-weight: 600; color: #f0f6fc; }
    .status-badge {
      font-size: 0.75rem;
      padding: 2px 8px;
      border-radius: 12px;
      background: #23863633;
      color: #3fb950;
      border: 1px solid #23863666;
    }
    .status-badge.running {
      background: #d2992233;
      color: #e3b341;
      border-color: #d2992266;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    .actions-top { display: flex; gap: 8px; align-items: center; }
    button {
      background: var(--card-bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 0.85rem;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
    }
    button:hover { background: #21262d; border-color: #8b949e; }
    button.primary { background: var(--accent); color: #fff; border-color: rgba(240,246,252,0.1); }
    button.primary:hover { background: var(--accent-hover); }
    button.danger { color: #f85149; }
    button.danger:hover { background: #b62324; color: #fff; border-color: #b62324; }

    /* NAVEGACIÓN POR PESTAÑAS */
    nav.tabs {
      background: var(--card-bg);
      border-bottom: 1px solid var(--border);
      display: flex;
      overflow-x: auto;
      padding: 0 16px;
    }
    nav.tabs button {
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      border-radius: 0;
      color: var(--text-muted);
      padding: 12px 16px;
      font-size: 0.9rem;
      white-space: nowrap;
    }
    nav.tabs button.active {
      color: #f0f6fc;
      border-bottom-color: #f78166;
      font-weight: 600;
    }

    .container {
      max-width: 1200px;
      margin: 20px auto;
      padding: 0 16px;
    }
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* TARJETAS Y GRID */
    .section-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      flex-wrap: wrap;
      gap: 10px;
    }
    .section-title { font-size: 1.1rem; font-weight: 600; color: #f0f6fc; }
    
    .products-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 16px;
    }
    .product-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: border-color 0.15s;
    }
    .product-card:hover { border-color: #8b949e; }
    .product-card.inactive { opacity: 0.6; border-style: dashed; }
    .card-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .prod-name { font-size: 1rem; font-weight: 600; color: #f0f6fc; }
    .prod-id { font-size: 0.75rem; color: var(--text-muted); font-family: monospace; }
    .prod-query {
      font-size: 0.85rem;
      background: #0d1117;
      padding: 4px 8px;
      border-radius: 4px;
      margin-bottom: 12px;
      word-break: break-all;
      color: var(--blue);
    }
    .prod-meta {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      font-size: 0.8rem;
      margin-bottom: 14px;
    }
    .meta-box {
      background: #0d1117;
      padding: 6px 10px;
      border-radius: 6px;
      border: 1px solid #21262d;
    }
    .meta-box .lbl { color: var(--text-muted); font-size: 0.7rem; display: block; }
    .meta-box .val { font-weight: 600; font-size: 0.9rem; color: #f0f6fc; }
    .card-actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-top: 10px;
      border-top: 1px solid #21262d;
    }
    .switch-label {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.8rem;
      cursor: pointer;
    }

    /* TABLA ESTILO GITHUB PARA HISTÓRICO */
    .table-container {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.85rem;
    }
    th, td {
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }
    th {
      background: #1c2128;
      color: #f0f6fc;
      font-weight: 600;
    }
    tr:hover td { background: #21262d44; }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge.si { background: #23863633; color: #3fb950; border: 1px solid #23863666; }
    .badge.no { background: #da363333; color: #f85149; border: 1px solid #da363366; }
    .btn-oferta {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      background: rgba(31, 111, 235, 0.15);
      color: #58a6ff;
      border: 1px solid rgba(88, 166, 255, 0.35);
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 500;
      text-decoration: none;
      transition: all 0.15s ease;
      white-space: nowrap;
    }
    .btn-oferta:hover {
      background: #1f6feb;
      color: #ffffff;
      border-color: #1f6feb;
      box-shadow: 0 0 8px rgba(31, 111, 235, 0.5);
    }
    .prod-link {
      color: #58a6ff;
      text-decoration: none;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .prod-link:hover {
      color: #79c0ff;
      text-decoration: underline;
    }
    .search-input {
      background: #0d1117;
      border: 1px solid var(--border);
      color: #fff;
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 0.85rem;
      width: 100%;
      max-width: 350px;
    }
    .search-input:focus { outline: none; border-color: var(--blue); }

    /* TERMINAL / LOGS */
    .terminal-box {
      background: #000;
      color: #58a6ff;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      padding: 14px;
      border-radius: 8px;
      border: 1px solid var(--border);
      font-size: 0.8rem;
      height: 480px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    /* MODAL */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.7);
      display: none;
      justify-content: center;
      align-items: center;
      z-index: 200;
      padding: 16px;
    }
    .modal {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      width: 100%;
      max-width: 520px;
      max-height: 90vh;
      overflow-y: auto;
      padding: 20px;
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }
    .modal-title { font-size: 1.1rem; font-weight: 600; }
    .form-group { margin-bottom: 14px; }
    .form-group label {
      display: block;
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-bottom: 4px;
      font-weight: 500;
    }
    .form-group input, .form-group select, .form-group textarea {
      width: 100%;
      background: #0d1117;
      border: 1px solid var(--border);
      color: #fff;
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 0.85rem;
    }
    .form-group input:focus { outline: none; border-color: var(--blue); }
    .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

    /* TOAST */
    #toast {
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: #1f6feb;
      color: #fff;
      padding: 10px 18px;
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 500;
      box-shadow: 0 4px 12px rgba(0,0,0,0.5);
      display: none;
      z-index: 300;
    }

    @media (max-width: 768px) {
      .products-grid { grid-template-columns: 1fr; }
      .form-row { grid-template-columns: 1fr; }
      header { flex-direction: column; align-items: stretch; }
      .actions-top { justify-content: space-between; }
    }
  </style>
</head>
<body>

  <header>
    <div class="logo-area">
      <h1>🔍 Monitor de Precios</h1>
      <span id="badge-interval" class="status-badge" style="background: rgba(88, 166, 255, 0.15); color: #58a6ff; border-color: rgba(88, 166, 255, 0.3);">⏱️ Cada 60m</span>
      <span id="badge-running" class="status-badge">Listo</span>
      <span id="badge-auth" class="status-badge" style="display:none; background: rgba(210, 153, 34, 0.15); color: #e3b341; border-color: rgba(210, 153, 34, 0.4);">🔒 Protegido</span>
      <span id="badge-gh" class="status-badge" style="display:none; background: rgba(139, 148, 158, 0.15); color: #c9d1d9; border-color: rgba(139, 148, 158, 0.4);">🐙 GitHub Sync</span>
    </div>
    <div class="actions-top">
      <button id="btn-sync-gh" onclick="sincronizarGitHub()" style="display:none;" title="Sincronizar cambios desde GitHub">
        <span>🐙</span> Sync GitHub
      </button>
      <button onclick="ejecutarMonitor()" class="primary" id="btn-run">
        <span>▶️</span> Comprobar Ahora
      </button>
      <button onclick="recargarDatos()">🔄 Refrescar</button>
    </div>
  </header>

  <nav class="tabs">
    <button class="active" onclick="cambiarPestaña('tab-productos', this)">📦 Productos</button>
    <button onclick="cambiarPestaña('tab-historico', this)">📊 Histórico CSV</button>
    <button onclick="cambiarPestaña('tab-estado', this)">🧠 Estado JSON</button>
    <button onclick="cambiarPestaña('tab-ajustes', this)">⚙️ Ajustes Globales</button>
    <button onclick="cambiarPestaña('tab-terminal', this)">💻 Terminal / Logs</button>
  </nav>

  <div class="container">

    <!-- 1. PESTAÑA PRODUCTOS -->
    <div id="tab-productos" class="tab-content active">
      <div class="section-header">
        <div>
          <h2 class="section-title">Productos en Monitorización</h2>
          <p style="font-size: 0.8rem; color: var(--text-muted);">
            Configura los productos, modelos y precios máximos para recibir alertas de Telegram.
          </p>
        </div>
        <button class="primary" onclick="abrirModalProducto()">➕ Añadir Producto</button>
      </div>

      <div id="grid-productos" class="products-grid">
        <p style="color: var(--text-muted);">Cargando productos...</p>
      </div>
    </div>

    <!-- 2. PESTAÑA HISTÓRICO CSV -->
    <div id="tab-historico" class="tab-content">
      <div class="section-header">
        <div>
          <h2 class="section-title">Histórico de Ofertas Detectadas</h2>
          <p id="total-historico" style="font-size: 0.8rem; color: var(--text-muted);">Cargando registros...</p>
        </div>
        <div style="display: flex; gap: 8px; width: 100%; max-width: 480px; justify-content: flex-end;">
          <input type="text" id="filtro-historico" class="search-input" placeholder="🔍 Filtrar por producto, tienda, país..." oninput="filtrarHistorico()">
          <button class="danger" onclick="limpiarHistorico()">🗑️ Vaciar</button>
        </div>
      </div>

      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>Fecha</th>
              <th>Producto</th>
              <th>Tienda</th>
              <th>País</th>
              <th>Precio</th>
              <th>Envío</th>
              <th>Total Unidad</th>
              <th>Cumple</th>
              <th style="min-width: 140px;">Enlace / Oferta</th>
            </tr>
          </thead>
          <tbody id="tabla-historico-body">
            <tr><td colspan="9" style="text-align: center; color: var(--text-muted);">Cargando historial...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 3. PESTAÑA ESTADO JSON -->
    <div id="tab-estado" class="tab-content">
      <div class="section-header">
        <div>
          <h2 class="section-title">Estado de Notificaciones Enviadas</h2>
          <p style="font-size: 0.8rem; color: var(--text-muted);">
            Ofertas que ya fueron notificadas para evitar spam. Puedes eliminar registros específicos para que vuelvan a alertar.
          </p>
        </div>
        <button class="danger" onclick="limpiarEstadoCompleto()">🗑️ Resetear Todo el Estado</button>
      </div>

      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>ID Oferta / Hash</th>
              <th>Último Precio Registrado</th>
              <th>Última Alerta Enviada</th>
              <th>Acción</th>
            </tr>
          </thead>
          <tbody id="tabla-estado-body">
            <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Cargando estado...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 4. PESTAÑA AJUSTES GLOBALES -->
    <div id="tab-ajustes" class="tab-content">
      <div class="section-header">
        <h2 class="section-title">Configuración General (config.json)</h2>
        <button class="primary" onclick="guardarAjustesGlobales()">💾 Guardar Ajustes</button>
      </div>

      <div class="product-card" style="max-width: 600px; margin-bottom: 20px;">
        <div class="form-row">
          <div class="form-group">
            <label>País Destino</label>
            <input type="text" id="cfg-pais" value="España">
          </div>
          <div class="form-group">
            <label>Moneda</label>
            <input type="text" id="cfg-moneda" value="EUR" readonly>
          </div>
        </div>

        <div class="form-group">
          <label>⏱️ Frecuencia de Rastreo Automático (minutos)</label>
          <input type="number" id="cfg-intervalo" min="5" max="1440" step="5" value="60">
          <small style="color: var(--text-muted); font-size: 0.75rem; display:block; margin-top:4px;">
            Intervalo de espera entre cada comprobación automática en el contenedor (ej. 15, 30, 60, 120 min). Se actualiza en vivo sin reiniciar Docker.
          </small>
        </div>

        <div class="form-row">
          <div class="form-group">
            <label>Envío Wallapop/Vinted (€)</label>
            <input type="number" step="0.5" id="cfg-envio-c2c" value="4.50">
          </div>
          <div class="form-group">
            <label>Envío Europa Estimado (€)</label>
            <input type="number" step="0.5" id="cfg-envio-eu" value="6.50">
          </div>
        </div>

        <div class="form-group">
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" id="cfg-solo-ue" checked>
            <span>Limitar estrictamente a la Unión Europea</span>
          </label>
        </div>
        <div class="form-group">
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" id="cfg-wallapop-vinted" checked>
            <span>Incluir marketplaces de segunda mano (Wallapop, Vinted)</span>
          </label>
        </div>
        <div class="form-group">
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" id="cfg-ignorar-vendidos" checked>
            <span>Ignorar anuncios vendidos o no disponibles</span>
          </label>
        </div>
        <div class="form-group">
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" id="cfg-incluir-envio" checked>
            <span>Sumar gastos de envío al calcular el precio objetivo</span>
          </label>
        </div>
      </div>
    </div>

    <!-- 5. PESTAÑA TERMINAL -->
    <div id="tab-terminal" class="tab-content">
      <div class="section-header">
        <div>
          <h2 class="section-title">Consola en Vivo del Monitor</h2>
          <p style="font-size: 0.8rem; color: var(--text-muted);">Salida en tiempo real del script monitor.py.</p>
        </div>
        <button onclick="limpiarConsola()">🧹 Limpiar Pantalla</button>
      </div>
      <div id="terminal" class="terminal-box">Esperando ejecución...</div>
    </div>

  </div>

  <!-- MODAL AÑADIR / EDITAR PRODUCTO -->
  <div id="modal-producto" class="modal-overlay">
    <div class="modal">
      <div class="modal-header">
        <h3 id="modal-producto-title" class="modal-title">Añadir Producto</h3>
        <button onclick="cerrarModalProducto()" style="border:none; background:none; font-size:1.2rem; cursor:pointer;">✕</button>
      </div>

      <form id="form-producto" onsubmit="guardarProducto(event)">
        <input type="hidden" id="prod-idx" value="-1">

        <div class="form-group">
          <label>ID Único (ej. wd-red-8tb, halo-2)</label>
          <input type="text" id="inp-id" required placeholder="identificador-unico">
        </div>

        <div class="form-group">
          <label>Nombre a mostrar</label>
          <input type="text" id="inp-nombre" required placeholder="WD Red Plus 8TB">
        </div>

        <div class="form-group">
          <label>Término de búsqueda / Código de modelo</label>
          <input type="text" id="inp-busqueda" required placeholder="WD80EFPX">
        </div>

        <div class="form-row">
          <div class="form-group">
            <label>Precio Máximo Unidad (€)</label>
            <input type="number" step="0.5" id="inp-precio-max" required placeholder="180">
          </div>
          <div class="form-group">
            <label>Cantidad en Pedido</label>
            <input type="number" id="inp-cantidad" value="1" min="1" required>
          </div>
        </div>

        <div class="form-row">
          <div class="form-group">
            <label>Capacidad (opcional)</label>
            <input type="number" step="0.1" id="inp-capacidad" placeholder="8">
          </div>
          <div class="form-group">
            <label>Unidad Medida</label>
            <select id="inp-unidad">
              <option value="">Ninguna</option>
              <option value="TB">TB</option>
              <option value="GB">GB</option>
              <option value="kg">kg</option>
              <option value="g">g</option>
              <option value="l">l</option>
            </select>
          </div>
        </div>

        <div class="form-group">
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; margin-top: 6px;">
            <input type="checkbox" id="inp-activo" checked>
            <span>Monitorizar este producto activamente</span>
          </label>
        </div>

        <div style="display:flex; justify-content: flex-end; gap: 10px; margin-top: 20px;">
          <button type="button" onclick="cerrarModalProducto()">Cancelar</button>
          <button type="submit" class="primary">Guardar Producto</button>
        </div>
      </form>
    </div>
  </div>

  <div id="toast">Acción completada</div>

  <script>
    let CONFIG_ACTUAL = { productos: [] };
    let HISTORIAL_ACTUAL = [];
    let ESTADO_ACTUAL = { ofertas: {} };

    // CAMBIAR PESTAÑAS
    function cambiarPestaña(tabId, btn) {
      document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('nav.tabs button').forEach(el => el.classList.remove('active'));
      document.getElementById(tabId).classList.add('active');
      btn.classList.add('active');
    }

    function mostrarToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3000);
    }

    // CARGAR DATOS AL INICIAR
    async function recargarDatos() {
      await Promise.all([cargarConfig(), cargarHistorial(), cargarEstado(), comprobarEstadoEjecucion()]);
      mostrarToast("Datos actualizados");
    }

    async function cargarConfig() {
      try {
        const res = await fetch('/api/config');
        CONFIG_ACTUAL = await res.json();
        renderizarProductos();
        renderizarAjustesGlobales();
      } catch (e) {
        console.error("Error al cargar config:", e);
      }
    }

    function renderizarProductos() {
      const container = document.getElementById('grid-productos');
      const prods = CONFIG_ACTUAL.productos || [];
      if (prods.length === 0) {
        container.innerHTML = '<p style="color:var(--text-muted); grid-column: 1/-1;">No hay productos configurados. Pulsa en "Añadir Producto".</p>';
        return;
      }
      container.innerHTML = prods.map((p, idx) => `
        <div class="product-card ${p.activo === false ? 'inactive' : ''}">
          <div>
            <div class="card-head">
              <div>
                <div class="prod-name">${escapeHtml(p.nombre || 'Sin nombre')}</div>
                <div class="prod-id">${escapeHtml(p.id || '')}</div>
              </div>
              <span class="badge ${p.activo !== false ? 'si' : 'no'}">${p.activo !== false ? 'Activo' : 'Pausado'}</span>
            </div>
            <div class="prod-query">🔍 ${escapeHtml(p.busqueda || '')}</div>
            <div class="prod-meta">
              <div class="meta-box">
                <span class="lbl">Precio Máximo</span>
                <span class="val">${p.precio_maximo_unidad ? p.precio_maximo_unidad + ' €' : 'Sin límite'}</span>
              </div>
              <div class="meta-box">
                <span class="lbl">Cantidad / Tamaño</span>
                <span class="val">${p.cantidad || 1} un. ${p.capacidad ? `(${p.capacidad} ${p.unidad_medida || ''})` : ''}</span>
              </div>
            </div>
          </div>
          <div class="card-actions">
            <label class="switch-label">
              <input type="checkbox" ${p.activo !== false ? 'checked' : ''} onchange="toggleActivoProducto(${idx}, this.checked)">
              <span>${p.activo !== false ? 'Pausar' : 'Activar'}</span>
            </label>
            <div style="display:flex; gap:6px;">
              <button onclick="editarProducto(${idx})">✏️ Editar</button>
              <button class="danger" onclick="eliminarProducto(${idx})">🗑️</button>
            </div>
          </div>
        </div>
      `).join('');
    }

    function renderizarAjustesGlobales() {
      document.getElementById('cfg-pais').value = CONFIG_ACTUAL.pais || 'España';
      document.getElementById('cfg-moneda').value = CONFIG_ACTUAL.moneda || 'EUR';
      const intervalo = CONFIG_ACTUAL.intervalo_minutos || 60;
      document.getElementById('cfg-intervalo').value = intervalo;
      const bInterval = document.getElementById('badge-interval');
      if (bInterval) bInterval.innerText = `⏱️ Cada ${intervalo}m`;
      document.getElementById('cfg-envio-c2c').value = CONFIG_ACTUAL.envio_wallapop_vinted_estimado || 4.50;
      document.getElementById('cfg-envio-eu').value = CONFIG_ACTUAL.envio_europa_estimado || 6.50;
      document.getElementById('cfg-solo-ue').checked = CONFIG_ACTUAL.solo_union_europea !== false;
      document.getElementById('cfg-wallapop-vinted').checked = CONFIG_ACTUAL.incluir_wallapop_vinted !== false;
      document.getElementById('cfg-ignorar-vendidos').checked = CONFIG_ACTUAL.ignorar_vendidos !== false;
      document.getElementById('cfg-incluir-envio').checked = CONFIG_ACTUAL.incluir_envio !== false;
    }

    async function guardarAjustesGlobales() {
      CONFIG_ACTUAL.pais = document.getElementById('cfg-pais').value;
      CONFIG_ACTUAL.intervalo_minutos = parseInt(document.getElementById('cfg-intervalo').value) || 60;
      const bInterval = document.getElementById('badge-interval');
      if (bInterval) bInterval.innerText = `⏱️ Cada ${CONFIG_ACTUAL.intervalo_minutos}m`;
      CONFIG_ACTUAL.envio_wallapop_vinted_estimado = parseFloat(document.getElementById('cfg-envio-c2c').value) || 4.50;
      CONFIG_ACTUAL.envio_europa_estimado = parseFloat(document.getElementById('cfg-envio-eu').value) || 6.50;
      CONFIG_ACTUAL.solo_union_europea = document.getElementById('cfg-solo-ue').checked;
      CONFIG_ACTUAL.incluir_wallapop_vinted = document.getElementById('cfg-wallapop-vinted').checked;
      CONFIG_ACTUAL.ignorar_vendidos = document.getElementById('cfg-ignorar-vendidos').checked;
      CONFIG_ACTUAL.incluir_envio = document.getElementById('cfg-incluir-envio').checked;

      await guardarConfigServidor();
    }

    async function toggleActivoProducto(idx, estado) {
      CONFIG_ACTUAL.productos[idx].activo = estado;
      await guardarConfigServidor();
      renderizarProductos();
    }

    function abrirModalProducto() {
      document.getElementById('prod-idx').value = "-1";
      document.getElementById('modal-producto-title').innerText = "Añadir Producto";
      document.getElementById('form-producto').reset();
      document.getElementById('inp-activo').checked = true;
      document.getElementById('modal-producto').style.display = 'flex';
    }

    function editarProducto(idx) {
      const p = CONFIG_ACTUAL.productos[idx];
      document.getElementById('prod-idx').value = idx;
      document.getElementById('modal-producto-title').innerText = "Editar Producto";
      document.getElementById('inp-id').value = p.id || '';
      document.getElementById('inp-nombre').value = p.nombre || '';
      document.getElementById('inp-busqueda').value = p.busqueda || '';
      document.getElementById('inp-precio-max').value = p.precio_maximo_unidad || '';
      document.getElementById('inp-cantidad').value = p.cantidad || 1;
      document.getElementById('inp-capacidad').value = p.capacidad || '';
      document.getElementById('inp-unidad').value = p.unidad_medida || '';
      document.getElementById('inp-activo').checked = p.activo !== false;
      document.getElementById('modal-producto').style.display = 'flex';
    }

    function cerrarModalProducto() {
      document.getElementById('modal-producto').style.display = 'none';
    }

    async function guardarProducto(e) {
      e.preventDefault();
      const idx = parseInt(document.getElementById('prod-idx').value);
      const prod = {
        id: document.getElementById('inp-id').value.trim(),
        nombre: document.getElementById('inp-nombre').value.trim(),
        busqueda: document.getElementById('inp-busqueda').value.trim(),
        precio_maximo_unidad: parseFloat(document.getElementById('inp-precio-max').value) || null,
        precio_maximo_pedido: null,
        precio_maximo_por_unidad_medida: null,
        cantidad: parseInt(document.getElementById('inp-cantidad').value) || 1,
        capacidad: parseFloat(document.getElementById('inp-capacidad').value) || null,
        unidad_medida: document.getElementById('inp-unidad').value || null,
        activo: document.getElementById('inp-activo').checked
      };

      if (!CONFIG_ACTUAL.productos) CONFIG_ACTUAL.productos = [];

      if (idx === -1) {
        CONFIG_ACTUAL.productos.push(prod);
      } else {
        CONFIG_ACTUAL.productos[idx] = prod;
      }

      await guardarConfigServidor();
      cerrarModalProducto();
      renderizarProductos();
    }

    async function eliminarProducto(idx) {
      if (!confirm(`¿Estás seguro de eliminar "${CONFIG_ACTUAL.productos[idx].nombre}"?`)) return;
      CONFIG_ACTUAL.productos.splice(idx, 1);
      await guardarConfigServidor();
      renderizarProductos();
    }

    async function guardarConfigServidor() {
      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(CONFIG_ACTUAL)
        });
        const d = await res.json();
        if (d.ok) {
          mostrarToast("Guardado con éxito");
        } else {
          alert("Error al guardar: " + d.error);
        }
      } catch (e) {
        alert("Error de red al guardar configuración");
      }
    }

    // CARGAR Y FILTRAR HISTORIAL
    async function cargarHistorial() {
      try {
        const res = await fetch('/api/history');
        HISTORIAL_ACTUAL = await res.json();
        filtrarHistorico();
      } catch (e) {
        console.error("Error al cargar historial:", e);
      }
    }

    function filtrarHistorico() {
      const q = document.getElementById('filtro-historico').value.toLowerCase().trim();
      const filtrados = HISTORIAL_ACTUAL.filter(r => {
        if (!q) return true;
        const texto = `${r.producto} ${r.tienda} ${r.pais} ${r.fecha} ${r.url || ''}`.toLowerCase();
        return texto.includes(q);
      });

      document.getElementById('total-historico').innerText = `${filtrados.length} registros encontrados (de un total de ${HISTORIAL_ACTUAL.length})`;

      const tbody = document.getElementById('tabla-historico-body');
      if (filtrados.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--text-muted);">No se encontraron ofertas.</td></tr>';
        return;
      }

      tbody.innerHTML = filtrados.slice(0, 150).map(r => {
        const fechaStr = r.fecha ? r.fecha.replace('T', ' ').substring(0, 19) : '-';
        const cumple = (r.cumple_objetivo || '').toUpperCase() === 'SI';
        const urlOferta = r.url && r.url.startsWith('http') ? r.url : '';
        return `
          <tr>
            <td style="font-size:0.75rem; color:var(--text-muted);">${fechaStr}</td>
            <td>
              ${urlOferta ? `
                <a href="${escapeHtml(urlOferta)}" target="_blank" rel="noopener noreferrer" class="prod-link" title="Abrir: ${escapeHtml(urlOferta)}">
                  ${escapeHtml(r.producto || 'Producto')} <span style="font-size:0.75rem;">↗</span>
                </a>
              ` : `<strong>${escapeHtml(r.producto || '')}</strong>`}
            </td>
            <td>${escapeHtml(r.tienda || '')}</td>
            <td>${escapeHtml(r.pais || '')}</td>
            <td>${r.precio_producto ? r.precio_producto + ' €' : '-'}</td>
            <td style="font-size:0.75rem;">${escapeHtml(r.envio || '-')}</td>
            <td style="font-weight:600;">${r.total_unidad_con_envio ? r.total_unidad_con_envio + ' €' : '-'}</td>
            <td><span class="badge ${cumple ? 'si' : 'no'}">${cumple ? 'SI' : 'NO'}</span></td>
            <td>
              ${urlOferta ? `
                <a href="${escapeHtml(urlOferta)}" target="_blank" rel="noopener noreferrer" class="btn-oferta" title="${escapeHtml(urlOferta)}">
                  <span>🔗 Abrir Oferta</span> ↗
                </a>
              ` : '<span style="color:var(--text-muted);">-</span>'}
            </td>
          </tr>
        `;
      }).join('');
    }

    async function limpiarHistorico() {
      if (!confirm("¿Deseas vaciar todo el registro de historico.csv?")) return;
      await fetch('/api/history', { method: 'DELETE' });
      await cargarHistorial();
      mostrarToast("Histórico vaciado");
    }

    // ESTADO DEL MONITOR
    async function cargarEstado() {
      try {
        const res = await fetch('/api/state');
        ESTADO_ACTUAL = await res.json();
        const ofertas = ESTADO_ACTUAL.ofertas || {};
        const tbody = document.getElementById('tabla-estado-body');
        const claves = Object.keys(ofertas);

        if (claves.length === 0) {
          tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No hay ofertas registradas en el estado actual.</td></tr>';
          return;
        }

        tbody.innerHTML = claves.map(k => {
          const item = ofertas[k];
          const fechaStr = item.ultima_alerta ? item.ultima_alerta.replace('T', ' ').substring(0, 19) : 'Sin alerta';
          return `
            <tr>
              <td style="font-family:monospace; font-size:0.75rem; color:var(--blue);">${escapeHtml(k.substring(0, 24))}...</td>
              <td>${item.precio ? item.precio + ' €' : '-'}</td>
              <td style="font-size:0.8rem; color:var(--text-muted);">${fechaStr}</td>
              <td><button class="danger" onclick="eliminarOfertaEstado('${k}')">Borrar</button></td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        console.error("Error al cargar estado:", e);
      }
    }

    async function eliminarOfertaEstado(clave) {
      delete ESTADO_ACTUAL.ofertas[clave];
      await fetch('/api/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(ESTADO_ACTUAL)
      });
      await cargarEstado();
      mostrarToast("Oferta eliminada del estado");
    }

    async function limpiarEstadoCompleto() {
      if (!confirm("¿Seguro que deseas resetear todo el estado? Se volverán a alertar ofertas si siguen activas.")) return;
      await fetch('/api/state', { method: 'DELETE' });
      await cargarEstado();
      mostrarToast("Estado reseteado");
    }

    // EJECUCIÓN MANUAL
    async function ejecutarMonitor() {
      const btn = document.getElementById('btn-run');
      btn.disabled = true;
      try {
        const res = await fetch('/api/run', { method: 'POST' });
        const d = await res.json();
        mostrarToast(d.mensaje || "Ejecución iniciada");
        // Cambiar automáticamente a la pestaña de terminal
        cambiarPestaña('tab-terminal', document.querySelectorAll('nav.tabs button')[4]);
      } catch (e) {
        alert("Error al iniciar comprobación");
      }
    }

    async function comprobarEstadoEjecucion() {
      try {
        const res = await fetch('/api/status');
        const st = await res.json();
        const badge = document.getElementById('badge-running');
        const btn = document.getElementById('btn-run');

        if (st.en_curso) {
          badge.innerText = "⏳ Rastreo en curso...";
          badge.className = "status-badge running";
          btn.disabled = true;
        } else {
          badge.innerText = "Listo";
          badge.className = "status-badge";
          btn.disabled = false;
        }

        const bAuth = document.getElementById('badge-auth');
        if (bAuth) bAuth.style.display = st.auth_activa ? 'inline-block' : 'none';

        const bGh = document.getElementById('badge-gh');
        const btnGh = document.getElementById('btn-sync-gh');
        if (bGh) {
          bGh.style.display = st.github_sync ? 'inline-block' : 'none';
          if (st.github_repo) bGh.title = 'Repo: ' + st.github_repo;
        }
        if (btnGh) btnGh.style.display = st.github_sync ? 'inline-flex' : 'none';

        const terminal = document.getElementById('terminal');
        if (st.logs && st.logs.length > 0) {
          terminal.innerText = st.logs.join('\n');
          terminal.scrollTop = terminal.scrollHeight;
        }
      } catch (e) {}
    }

    async function sincronizarGitHub() {
      const btn = document.getElementById('btn-sync-gh');
      btn.disabled = true;
      const originalText = btn.innerHTML;
      btn.innerText = "⏳ Sincronizando...";
      try {
        const res = await fetch('/api/github/sync', { method: 'POST' });
        const d = await res.json();
        if (d.ok) {
          mostrarToast(d.mensaje || "Sincronizado desde GitHub");
          await cargarConfig();
          await cargarHistorial();
          await cargarEstado();
        } else {
          alert("⚠️ " + (d.mensaje || d.error || "No se pudo sincronizar"));
        }
      } catch (e) {
        alert("Error de conexión al sincronizar con GitHub: " + e);
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
      }
    }

    function limpiarConsola() {
      document.getElementById('terminal').innerText = "Consola limpia.";
    }

    function escapeHtml(str) {
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Sondeo de estado cada 2 segundos
    setInterval(comprobarEstadoEjecucion, 2500);

    // Inicialización
    window.addEventListener('DOMContentLoaded', () => {
      cargarConfig();
      cargarHistorial();
      cargarEstado();
      comprobarEstadoEjecucion();
    });
  </script>
</body>
</html>
"""


# ============================================================
# CONTROLADOR HTTP (API REST Y FRONTEND)
# ============================================================

class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silenciar logs ruidosos en stdout
        return

    def do_HEAD(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        if path in ("", "/index.html", "/healthz", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            return
        if not self.check_auth():
            return
        self.send_response(200)
        self.end_headers()

    def check_auth(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        # Los endpoints de comprobación de salud nunca requieren autenticación (vital para Render / Cloud)
        if path in ("/healthz", "/health"):
            return True

        if not AUTH_ENABLED:
            return True
        auth_header = self.headers.get("Authorization")
        if auth_header and auth_header.startswith("Basic "):
            try:
                encoded = auth_header.split(" ", 1)[1].strip()
                decoded = base64.b64decode(encoded).decode("utf-8")
                user, passwd = decoded.split(":", 1)
                if user == AUTH_USER and passwd == AUTH_PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Price Monitor"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"ok": false, "error": "401 Unauthorized: Credenciales incorrectas o ausentes."}')
        self.wfile.flush()
        return False

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")

        # Health check para Render, Fly.io, Kubernetes y balanceadores
        if path in ("/healthz", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
            self.wfile.flush()
            return

        if not self.check_auth():
            return

        if path == "" or path == "/index.html":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return

        if path == "/api/config":
            self.send_json(leer_config())
            return

        if path == "/api/history":
            self.send_json(leer_historial())
            return

        if path == "/api/state":
            self.send_json(leer_estado())
            return

        if path == "/api/status":
            with EJECUCION_LOCK:
                st = dict(ESTADO_EJECUCION)
                st["auth_activa"] = AUTH_ENABLED
                st["github_sync"] = github_sync.esta_activo() if github_sync else False
                st["github_repo"] = github_sync.GITHUB_REPO if (github_sync and github_sync.esta_activo()) else None
                self.send_json(st)
            return

        self.send_error(404, "Ruta no encontrada")

    def do_POST(self):
        if not self.check_auth():
            return

        url = urlparse(self.path)
        path = url.path.rstrip("/")

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        if path == "/api/config":
            try:
                datos = json.loads(post_data.decode("utf-8"))
                ok, msg = guardar_config(datos)
                self.send_json({"ok": ok, "mensaje": msg}, status=200 if ok else 400)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=400)
            return

        if path == "/api/state":
            try:
                datos = json.loads(post_data.decode("utf-8"))
                ok, msg = guardar_estado(datos)
                self.send_json({"ok": ok, "mensaje": msg}, status=200 if ok else 400)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=400)
            return

        if path == "/api/github/sync":
            if not (github_sync and github_sync.esta_activo()):
                self.send_json({"ok": False, "mensaje": "GitHub Sync no está configurado (faltan variables GITHUB_TOKEN y GITHUB_REPO)."}, status=400)
                return
            try:
                ok = github_sync.pull_todos()
                self.send_json({
                    "ok": ok,
                    "mensaje": "Sincronización con GitHub completada exitosamente." if ok else "No se pudo sincronizar algunos archivos desde GitHub."
                })
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/run":
            with EJECUCION_LOCK:
                if ESTADO_EJECUCION["en_curso"]:
                    self.send_json({"ok": False, "mensaje": "Ya hay una comprobación en curso."}, status=409)
                    return
            t = threading.Thread(target=ejecutar_monitor_hilo, daemon=True)
            t.start()
            self.send_json({"ok": True, "mensaje": "Comprobación del monitor iniciada."})
            return

        self.send_error(404, "Ruta no encontrada")

    def do_DELETE(self):
        if not self.check_auth():
            return

        url = urlparse(self.path)
        path = url.path.rstrip("/")

        if path == "/api/history":
            try:
                # Vaciar conservando la cabecera
                cabecera = "fecha,producto_id,producto,tienda,pais,precio_producto,envio,envio_confirmado,total_unidad_con_envio,cantidad,total_pedido_con_envio,precio_unidad_medida,cumple_objetivo,url\n"
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    f.write(cabecera)
                if github_sync and github_sync.esta_activo():
                    threading.Thread(
                        target=github_sync.subir_archivo_github,
                        args=("historico.csv", HISTORY_FILE, "Web: Vaciar historico.csv [skip ci]"),
                        daemon=True
                    ).start()
                self.send_json({"ok": True, "mensaje": "Historial vaciado"})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/state":
            try:
                ok, msg = guardar_estado({"ofertas": {}})
                self.send_json({"ok": ok, "mensaje": msg}, status=200 if ok else 500)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)
            return

        self.send_error(404, "Ruta no encontrada")


def run_server(port=9999):
    server_address = ("0.0.0.0", port)
    httpd = ThreadingHTTPServer(server_address, WebHandler)
    print("=" * 60)
    print(f"🚀 PANEL WEB ACTIVO en http://0.0.0.0:{port}")
    print(f"🔒 Autenticación: {'Activada (Usuario: ' + AUTH_USER + ')' if AUTH_ENABLED else 'Desactivada (Público/Red Local)'}")
    print(f"🐙 GitHub Sync:   {'Activo (' + github_sync.GITHUB_REPO + ')' if (github_sync and github_sync.esta_activo()) else 'Desactivado (Modo local)'}")
    print(f"📁 Configuración: {CONFIG_FILE}")
    print(f"📁 Histórico:     {HISTORY_FILE}")
    print(f"📁 Estado:        {STATE_FILE}")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo servidor web...")
        httpd.server_close()


if __name__ == "__main__":
    # PORT (proporcionado por Render/Fly.io/Heroku) tiene prioridad máxima sobre WEB_PORT
    puerto = int(os.getenv("PORT", os.getenv("WEB_PORT", 9999)))
    run_server(puerto)
