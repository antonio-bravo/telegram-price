"""
Módulo de Sincronización con GitHub (Opcional).
Permite mantener sincronizados config.json, historico.csv y monitor_state.json
directamente con el repositorio de GitHub cuando el contenedor se ejecuta
en entornos efímeros o en la nube (como Render.com, Railway, Fly.io, etc.).

Variables de entorno configurables en .env o en el panel de Render:
- GITHUB_TOKEN: Personal Access Token (classic con permiso 'repo' o fine-grained con 'contents:write')
- GITHUB_REPO: "usuario/repositorio" (ej. "antonio-bravo/telegram-price")
- GITHUB_BRANCH: Rama de trabajo (por defecto: "main")
"""

import os
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()

def esta_activo():
    """Indica si la sincronización con GitHub está configurada y activa."""
    return bool(GITHUB_TOKEN and GITHUB_REPO)

def obtener_info_archivo_remoto(nombre_archivo):
    """Consulta la API de GitHub para obtener el SHA y metadatos de un archivo."""
    if not esta_activo():
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{nombre_archivo}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "PriceMonitor-CloudSync"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"⚠️ [GitHub Sync] Error HTTP {e.code} al consultar {nombre_archivo}: {e}")
    except Exception as e:
        print(f"⚠️ [GitHub Sync] Error al consultar {nombre_archivo}: {e}")
    return None

def descargar_archivo_github(nombre_archivo, ruta_local):
    """Descarga un archivo desde GitHub y lo guarda en la ruta local."""
    if not esta_activo():
        return False
    data = obtener_info_archivo_remoto(nombre_archivo)
    if not data or "content" not in data:
        return False
    try:
        raw_bytes = base64.b64decode(data["content"].encode("utf-8"))
        p = Path(ruta_local)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(raw_bytes)
        print(f"📥 [GitHub Sync] '{nombre_archivo}' descargado correctamente desde GitHub.")
        return True
    except Exception as e:
        print(f"⚠️ [GitHub Sync] Error al guardar {nombre_archivo} localmente: {e}")
        return False

def subir_archivo_github(nombre_archivo, ruta_local, commit_message="Actualizar archivo [skip ci]"):
    """Sube un archivo local a GitHub haciendo commit en la rama configurada."""
    if not esta_activo():
        return False
    p = Path(ruta_local)
    if not p.exists():
        return False
    try:
        with open(p, "rb") as f:
            contenido_bytes = f.read()
        b64_content = base64.b64encode(contenido_bytes).decode("utf-8")
        
        info_remoto = obtener_info_archivo_remoto(nombre_archivo)
        sha = info_remoto.get("sha") if info_remoto else None

        payload = {
            "message": commit_message,
            "content": b64_content,
            "branch": GITHUB_BRANCH
        }
        if sha:
            payload["sha"] = sha

        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{nombre_archivo}"
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=req_data, method="PUT", headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "PriceMonitor-CloudSync"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status in (200, 201):
                print(f"📤 [GitHub Sync] '{nombre_archivo}' subido a GitHub exitosamente ({commit_message}).")
                return True
    except Exception as e:
        print(f"⚠️ [GitHub Sync] Error al subir {nombre_archivo} a GitHub: {e}")
    return False

def pull_todos():
    """Descarga de GitHub config.json, historico.csv y monitor_state.json."""
    if not esta_activo():
        return False
    from pathlib import Path
    data_dir = Path(os.getenv("DATA_DIR", "/app/data" if Path("/app/data").exists() else "."))
    cfg_file = Path(os.getenv("CONFIG_PATH", data_dir / "config.json"))
    state_file = Path(os.getenv("STATE_PATH", data_dir / "monitor_state.json"))
    hist_file = Path(os.getenv("HISTORY_PATH", data_dir / "historico.csv"))

    print(f"🐙 [GitHub Sync] Sincronizando desde https://github.com/{GITHUB_REPO} (rama: {GITHUB_BRANCH})...")
    ok1 = descargar_archivo_github("config.json", cfg_file)
    ok2 = descargar_archivo_github("monitor_state.json", state_file)
    ok3 = descargar_archivo_github("historico.csv", hist_file)
    return ok1 or ok2 or ok3

if __name__ == "__main__":
    import sys
    accion = sys.argv[1] if len(sys.argv) > 1 else "status"
    if accion == "pull":
        pull_todos()
    else:
        print(f"GitHub Sync activo: {esta_activo()} (Repo: {GITHUB_REPO or 'No configurado'})")
