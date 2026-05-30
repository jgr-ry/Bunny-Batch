#!/usr/bin/env python3
"""
CursoFetch — estudio educativo de flujos HLS y automatizacion de descargas.

Caso de uso documentado: plataformas que revenden cursos premium sin licencia
(patron generico Supabase + CDN + catalogo). No apunta a ninguna marca concreta.

Un solo archivo autonomo: doble clic en cursofetch.py
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
    from rich.theme import Theme
    from rich import box

    RICH_UI = True
except ImportError:
    RICH_UI = False

APP_THEME = Theme(
    {
        "banner": "bold magenta",
        "accent": "bold cyan",
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "muted": "dim white",
        "menu": "white",
        "highlight": "bold white on dark_blue",
    }
) if RICH_UI else None

console: Console | None = None


def setup_windows_console() -> None:
    if sys.platform != "win32":
        return
    try:
        import colorama

        colorama.just_fix_windows_console()
    except ImportError:
        pass
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def init_console() -> None:
    global console
    if not RICH_UI:
        console = None
        return
    from rich.console import Console

    console = Console(theme=APP_THEME, highlight=False, force_terminal=True)


APP_NAME = "CursoFetch"
APP_SCRIPT = "cursofetch.py"
APP_VERSION = "2.0.0"
DEFAULT_VIDEO_DURATION = 300.0
DOWNLOAD_SPEED_FACTOR = 0.45

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
COURSES_DIR = DATA_DIR / "cursos"
IMPORT_DIR = DATA_DIR / "import"
DOWNLOADS_DIR = APP_DIR / "descargas"
ASSETS_DIR = APP_DIR / "assets"
SCRIPTS_DIR = ASSETS_DIR / "scripts"

DEFAULT_LIST = COURSES_DIR / "_ultimo" / "videos.txt"
DEFAULT_OUTPUT = DOWNLOADS_DIR
DEFAULT_TOKEN = DATA_DIR / "session_token.txt"
FAILED_VIDEOS = DATA_DIR / "failed_videos.txt"
REQUIREMENTS_FILE = ASSETS_DIR / "requirements.txt"
EXTRACT_SCRIPT = SCRIPTS_DIR / "extract_catalog.js"
GUIDE_FILE = ASSETS_DIR / "GUIA_EXTRACCION.txt"

IMPORT_SERVER_HOST = "127.0.0.1"
IMPORT_SERVER_PORT = 18765
_active_import_port: int | None = None
_import_server: socketserver.TCPServer | None = None
_import_server_thread: threading.Thread | None = None
_import_notify_lock = threading.Lock()

EMBEDDED_REQUIREMENTS = "rich>=13.7.0\ncolorama>=0.4.6\n"

LEGACY_ROOT_FILES = (
    "videos.json",
    "videos.txt",
    "session_token.txt",
    "failed_videos.txt",
    "extract_catalog.js",
    "extract_robinhood.js",
    "extract_all_videos.js",
    "requirements.txt",
    "_mods.json",
    "catalogo.json",
)

UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
DEFAULT_CDN = "vz-b011f47e-228.b-cdn.net"
DEFAULT_REFERER = "https://iframe.mediadelivery.net/"
FALLBACK_REFERERS = (
    "https://iframe.mediadelivery.net/",
)
QUALITIES = ("1080p", "720p", "480p", "360p", "240p")
QUALITY_MODES = ("best", "auto", *QUALITIES)

_M3U8_CACHE: dict[tuple[str, str, str], tuple[str, str]] = {}
_M3U8_CACHE_LOCK = threading.Lock()

PLATFORM_SUPABASE_URL = "https://avyvlzfqeyybjdevgwip.supabase.co"
PLATFORM_API_KEY = "sb_publishable_gDSiA4fwKNisficNGUZMFA_6iZBv0dC"

EXAMPLE_CASE_STUDY = """
CONTEXTO DEL EJEMPLO (caso de estudio generico):
  Existen sitios que agrupan cursos de alto valor y los comercializan sin
  licencia del titular original. CursoFetch documenta el PATRON TECNICO
  (login + API + HLS) con fines de investigacion educativa sobre debilidades
  en la distribucion de video — no como herramienta contra ninguna web concreta.
"""

LOGIN_REQUIRED_HINT = """
Esta plataforma de ejemplo requiere INICIO DE SESION (patron Supabase + SPA).

METODO RECOMENDADO (navegador + app abierta):
  1. Abre {APP_SCRIPT} y DEJALO ABIERTO (activa el servidor local)
  2. Inicia sesion en la plataforma de prueba
  3. Abre el catalogo del curso (/catalog/... o /app/catalog/...)
  4. F12 -> Consola -> pega el script extract_catalog.js
  5. El curso se guarda SOLO en data/cursos/ (sin ZIP manual)
  6. En la app elige opcion 3 (Descargar curso)

METODO ALTERNATIVO (sin app abierta):
  El script descarga un ZIP -> copialo a data/import/

METODO ALTERNATIVO (token):
  1. F12 -> Application -> Local Storage -> auth-token
  2. Copia access_token en data/session_token.txt
  3. Usa opcion 6 del menu (Plataforma con login)
"""


def sanitize_dir_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip(".") or "curso"


def course_dir_from_json(path: Path) -> Path | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    course = (data.get("course") or data.get("curso") or "").strip()
    slug = (data.get("slug") or "").strip()
    folder = sanitize_dir_name(course or slug or path.parent.name)
    return COURSES_DIR / folder


def discover_courses() -> list[dict]:
    courses: list[dict] = []
    if not COURSES_DIR.exists():
        return courses

    for json_path in sorted(COURSES_DIR.rglob("videos.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = (data.get("course") or data.get("curso") or json_path.parent.name).strip()
        stats = data.get("stats") or {}
        videos = stats.get("videos")
        if videos is None:
            videos = sum(
                len(mod.get("lessons") or mod.get("lecciones") or [])
                for mod in (data.get("modules") or data.get("modulos") or [])
            )
        generated = data.get("generated_at") or ""
        mtime = json_path.stat().st_mtime
        courses.append(
            {
                "name": name,
                "slug": data.get("slug") or "",
                "path": json_path,
                "dir": json_path.parent,
                "videos": int(videos or 0),
                "modules": stats.get("modules") or len(data.get("modules") or []),
                "generated_at": generated,
                "mtime": mtime,
            }
        )

    courses.sort(key=lambda item: item["mtime"], reverse=True)
    return courses


def import_zip_bundles() -> list[str]:
    imported: list[str] = []
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    COURSES_DIR.mkdir(parents=True, exist_ok=True)

    for zip_path in sorted(IMPORT_DIR.glob("*.zip")):
        try:
            import zipfile

            with zipfile.ZipFile(zip_path) as archive:
                names = [n for n in archive.namelist() if n and not n.endswith("/")]
                if not names:
                    continue
                top_levels = {n.split("/")[0] for n in names if "/" in n}
                has_nested = bool(top_levels)
                if has_nested and len(top_levels) == 1:
                    folder_name = sanitize_dir_name(next(iter(top_levels)))
                    archive.extractall(COURSES_DIR)
                    target_root = COURSES_DIR / folder_name
                else:
                    target_root = COURSES_DIR / sanitize_dir_name(zip_path.stem.replace(" - exportacion", ""))
                    target_root.mkdir(parents=True, exist_ok=True)
                    archive.extractall(target_root)
            done_dir = IMPORT_DIR / "procesados"
            done_dir.mkdir(exist_ok=True)
            dest = done_dir / zip_path.name
            if dest.exists():
                dest = done_dir / f"{zip_path.stem}_{int(time.time())}{zip_path.suffix}"
            zip_path.replace(dest)
            imported.append(target_root.name)
        except Exception as exc:
            ui_print(f"[warn]No se pudo importar {zip_path.name}:[/warn] {exc}", "warn")
    return imported


def save_course_bundle_from_browser(bundle: dict, text: str) -> Path:
    course = (bundle.get("course") or bundle.get("curso") or "curso").strip()
    folder = COURSES_DIR / sanitize_dir_name(course)
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / "videos.json"
    txt_path = folder / "videos.txt"
    json_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    normalized = text if text.endswith("\n") else text + "\n"
    txt_path.write_text(normalized, encoding="utf-8")
    return folder


def notify_browser_import(folder: Path, course_name: str, videos: int | None) -> None:
    with _import_notify_lock:
        detail = f"{videos} videos" if videos is not None else "curso"
        message = f"Curso recibido desde navegador: {course_name} ({detail})"
        try:
            if ui_enabled() and console:
                console.print(
                    f"\n[ok]{message}[/ok]\n[muted]  -> {folder}[/muted]\n",
                )
            else:
                print(f"\n{message}\n  -> {folder}\n")
        except Exception:
            print(f"\n{message}\n  -> {folder}\n")


class ImportRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json_response(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._json_response(
                200,
                {
                    "ok": True,
                    "app_dir": str(APP_DIR),
                    "courses_dir": str(COURSES_DIR.resolve()),
                    "port": _active_import_port or IMPORT_SERVER_PORT,
                },
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/import":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            bundle = data.get("bundle") or data
            text = data.get("text") or ""
            if not isinstance(bundle, dict) or not bundle.get("modules"):
                self._json_response(400, {"ok": False, "error": "Datos del curso invalidos"})
                return
            folder = save_course_bundle_from_browser(bundle, text)
            course_name = (bundle.get("course") or bundle.get("curso") or folder.name).strip()
            videos = (bundle.get("stats") or {}).get("videos")
            notify_browser_import(folder, course_name, videos)
            self._json_response(
                200,
                {
                    "ok": True,
                    "path": str(folder.resolve()),
                    "course": course_name,
                    "videos": videos,
                },
            )
        except Exception as exc:
            self._json_response(500, {"ok": False, "error": str(exc)})


def start_import_server() -> int | None:
    global _import_server, _import_server_thread, _active_import_port
    if _import_server is not None and _active_import_port is not None:
        return _active_import_port

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    for port in range(IMPORT_SERVER_PORT, IMPORT_SERVER_PORT + 10):
        try:
            server = ReusableTCPServer((IMPORT_SERVER_HOST, port), ImportRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True, name="import-server")
            thread.start()
            _import_server = server
            _import_server_thread = thread
            _active_import_port = port
            return port
        except OSError:
            continue
    return None


def stop_import_server() -> None:
    global _import_server, _import_server_thread, _active_import_port
    if _import_server is not None:
        _import_server.shutdown()
        _import_server.server_close()
    _import_server = None
    _import_server_thread = None
    _active_import_port = None


def import_server_url() -> str | None:
    if _active_import_port is None:
        return None
    return f"http://{IMPORT_SERVER_HOST}:{_active_import_port}"


def migrate_legacy_layout() -> list[str]:
    moved: list[str] = []
    COURSES_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    legacy_json = APP_DIR / "videos.json"
    if legacy_json.exists():
        target_dir = course_dir_from_json(legacy_json) or COURSES_DIR / "curso_importado"
        target_dir.mkdir(parents=True, exist_ok=True)
        if not (target_dir / "videos.json").exists():
            legacy_json.replace(target_dir / "videos.json")
            moved.append(str(target_dir / "videos.json"))
        legacy_txt = APP_DIR / "videos.txt"
        if legacy_txt.exists() and not (target_dir / "videos.txt").exists():
            legacy_txt.replace(target_dir / "videos.txt")
            moved.append(str(target_dir / "videos.txt"))

    for name, dest in (
        ("session_token.txt", DEFAULT_TOKEN),
        ("failed_videos.txt", FAILED_VIDEOS),
    ):
        src = APP_DIR / name
        if src.exists() and not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
            moved.append(str(dest))

    catalogo = APP_DIR / "catalogo.json"
    if catalogo.exists():
        dest = DATA_DIR / "catalogo.json"
        if not dest.exists():
            catalogo.replace(dest)
            moved.append(str(dest))

    for name in LEGACY_ROOT_FILES:
        src = APP_DIR / name
        if src.exists() and name not in {"videos.json", "videos.txt", "session_token.txt", "failed_videos.txt", "catalogo.json"}:
            try:
                src.unlink()
                moved.append(f"eliminado:{name}")
            except OSError:
                pass

    return moved


def build_extract_catalog_js() -> str:
    return f'''/**
 * Exportador de catalogo — caso de estudio {APP_NAME} v{APP_VERSION}
 * Patron generico: plataforma con login + Supabase + videos HLS en CDN.
 * 1. Inicia sesion en la plataforma de prueba (cualquier origen /catalog/)
 * 2. Abre el catalogo del curso
 * 3. F12 -> Consola -> pega TODO -> Enter
 * 4. Con la app abierta: se guarda solo en data/cursos/
 * 5. Sin app abierta: fallback ZIP -> data/import/
 */
(async () => {{
  const SUPABASE_URL = "{PLATFORM_SUPABASE_URL}";
  const API_KEY = "{PLATFORM_API_KEY}";
  const DEFAULT_CDN = "{DEFAULT_CDN}";
  const APP_VERSION = "{APP_VERSION}";
  const IMPORT_SERVER = "http://{IMPORT_SERVER_HOST}:{IMPORT_SERVER_PORT}";
  const UUID_RE =
    /[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}/i;
  const CDN_RE = /(vz-[a-z0-9-]+\\.b-cdn\\.net)/i;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const sanitizeName = (value) =>
    String(value || "curso")
      .replace(/[<>:"/\\\\|?*]/g, "")
      .replace(/\\s+/g, " ")
      .trim()
      .replace(/\\.$/, "") || "curso";

  const formatModuleFolder = (orden, nombre) => {{
    const n = String(nombre || "Sin nombre").trim();
    if (orden === null || orden === undefined || Number.isNaN(Number(orden))) return n;
    const num = Number(orden);
    const prefix = num < 0 ? String(num) : String(num).padStart(2, "0");
    return `${{prefix}} - ${{n}}`;
  }};

  const extractVideoMeta = (embed) => {{
    if (!embed) return null;
    const idMatch = embed.match(UUID_RE);
    if (!idMatch) return null;
    const cdnMatch = embed.match(CDN_RE);
    return {{
      video_id: idMatch[0].toLowerCase(),
      cdn: cdnMatch ? cdnMatch[1].toLowerCase() : DEFAULT_CDN,
      embed_url: embed,
    }};
  }};

  const getAccessToken = () => {{
    const storageKey = Object.keys(localStorage).find((k) => k.includes("auth-token"));
    if (!storageKey) return null;
    try {{
      return JSON.parse(localStorage.getItem(storageKey)).access_token || null;
    }} catch {{
      return null;
    }}
  }};

  const fetchJson = async (url, headers, label) => {{
    const res = await fetch(url, {{ headers }});
    const text = await res.text();
    let data;
    try {{
      data = text ? JSON.parse(text) : null;
    }} catch {{
      throw new Error(`${{label}}: respuesta invalida (${{res.status}})`);
    }}
    if (!res.ok) {{
      const msg = data?.message || data?.error || text.slice(0, 200);
      throw new Error(`${{label}}: HTTP ${{res.status}} - ${{msg}}`);
    }}
    return data;
  }};

  const getEmbedUrl = async (lesson, headers, retries = 3) => {{
    if (lesson.video_embed_url) return lesson.video_embed_url;
    for (let attempt = 1; attempt <= retries; attempt++) {{
      try {{
        const res = await fetch(`${{SUPABASE_URL}}/functions/v1/get-video-embed`, {{
          method: "POST",
          headers,
          body: JSON.stringify({{ lessonId: lesson.id }}),
        }});
        const data = await res.json().catch(() => ({{}}));
        if (res.ok) return data.embedUrl || data.embed_url || null;
        if (res.status === 401 || res.status === 403) {{
          throw new Error("Sesion expirada. Vuelve a iniciar sesion.");
        }}
      }} catch (err) {{
        if (attempt === retries) throw err;
      }}
      await sleep(400 * attempt);
    }}
    return null;
  }};

  const downloadBlob = (filename, blob) =>
    new Promise((resolve) => {{
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setTimeout(resolve, 500);
    }});

  const loadJSZip = () =>
    new Promise((resolve, reject) => {{
      if (window.JSZip) return resolve(window.JSZip);
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js";
      script.onload = () => resolve(window.JSZip);
      script.onerror = () => reject(new Error("No se pudo cargar JSZip"));
      document.head.appendChild(script);
    }});

  const sendToLocalApp = async (bundle, txt) => {{
    try {{
      const res = await fetch(`${{IMPORT_SERVER}}/import`, {{
        method: "POST",
        mode: "cors",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ bundle, text: txt }}),
      }});
      if (res.ok) return await res.json();
      console.warn("Import local HTTP", res.status, await res.text());
    }} catch (err) {{
      console.warn("App local no detectada (abre {APP_SCRIPT} y dejalo abierto):", err);
    }}
    return null;
  }};

  const accessToken = getAccessToken();
  if (!accessToken) {{
    alert("No hay sesion activa. Inicia sesion y abre el catalogo del curso.");
    return;
  }}

  const slug = location.pathname.split("/").filter(Boolean).pop();
  if (!slug || !location.pathname.includes("/catalog/")) {{
    alert("Abre la pagina del catalogo (/app/catalog/...), no un video suelto.");
    return;
  }}

  const headers = {{
    apikey: API_KEY,
    Authorization: `Bearer ${{accessToken}}`,
    "Content-Type": "application/json",
  }};

  console.log(`%c{APP_NAME} v${{APP_VERSION}}`, "color:#0ff;font-weight:bold");
  console.log(`Extrayendo curso: ${{slug}}`);

  let courses;
  let modules;
  try {{
    courses = await fetchJson(
      `${{SUPABASE_URL}}/rest/v1/course_summaries?slug=eq.${{encodeURIComponent(slug)}}&select=id,slug,nombre`,
      headers,
      "Curso"
    );
    if (!courses?.length) {{
      alert("Curso no encontrado. Comprueba la URL del catalogo.");
      return;
    }}
    const courseRef = courses[0];
    modules = await fetchJson(
      `${{SUPABASE_URL}}/rest/v1/modules?curso_id=eq.${{courseRef.id}}` +
        `&select=id,nombre,orden,` +
        `lessons:lessons(id,nombre,orden,video_embed_url,duracion_segundos)` +
        `&order=orden.asc`,
      headers,
      "Modulos"
    );
  }} catch (err) {{
    alert(String(err.message || err));
    console.error(err);
    return;
  }}

  const course = courses[0];
  const courseFolder = sanitizeName(course.nombre.trim());
  const sortedModules = (modules || []).sort((a, b) => a.orden - b.orden);
  const lines = [`# CURSO: ${{course.nombre.trim()}}`];
  const jsonModules = [];
  const failed = [];
  let ok = 0;

  for (const mod of sortedModules) {{
    const modFolder = formatModuleFolder(mod.orden, mod.nombre);
    const lessons = (mod.lessons || []).sort((a, b) => a.orden - b.orden);
    const jsonLessons = [];

    for (let i = 0; i < lessons.length; i++) {{
      const lesson = lessons[i];
      const orderInModule = i + 1;
      let embed;
      try {{
        embed = await getEmbedUrl(lesson, headers);
        await sleep(120);
      }} catch (err) {{
        failed.push({{ module: modFolder, lesson: lesson.nombre, error: String(err.message || err) }});
        continue;
      }}
      const meta = extractVideoMeta(embed);
      if (!meta) {{
        failed.push({{ module: modFolder, lesson: lesson.nombre, error: "Sin URL de video" }});
        continue;
      }}
      lines.push(`${{modFolder}} | ${{lesson.nombre}} | ${{meta.video_id}}`);
      jsonLessons.push({{
        title: lesson.nombre,
        nombre: lesson.nombre,
        video_id: meta.video_id,
        cdn: meta.cdn,
        embed_url: meta.embed_url,
        lesson_id: lesson.id,
        order_in_module: orderInModule,
        orden: lesson.orden,
        duracion_segundos: lesson.duracion_segundos || null,
      }});
      ok++;
    }}

    if (jsonLessons.length) {{
      jsonModules.push({{
        folder: modFolder,
        nombre: mod.nombre,
        orden: mod.orden,
        lessons: jsonLessons,
      }});
    }}
  }}

  const jsonData = {{
    version: 2,
    generated_at: new Date().toISOString(),
    course: course.nombre.trim(),
    curso: course.nombre.trim(),
    slug: course.slug,
    default_cdn: DEFAULT_CDN,
    stats: {{ modules: jsonModules.length, videos: ok, failed: failed.length }},
    modules: jsonModules,
    failed,
  }};

  const text = lines.join("\\n") + "\\n";
  const jsonText = JSON.stringify(jsonData, null, 2) + "\\n";
  const readme =
    `Exportacion {APP_NAME} v${{APP_VERSION}}\\n` +
    `========================================\\n\\n` +
    `Curso: ${{course.nombre.trim()}}\\n` +
    `Slug: ${{course.slug}}\\n` +
    `Videos: ${{ok}} | Modulos: ${{jsonModules.length}}\\n\\n` +
    `PASOS:\\n` +
    `1. Extrae este ZIP completo\\n` +
    `2. Copia el ZIP a la carpeta data/import/ junto a {APP_SCRIPT}\\n` +
    `3. Ejecuta {APP_SCRIPT} y elige opcion 3 (Descargar curso)\\n`;

  try {{ copy(text); }} catch {{ /* Firefox */ }}

  const local = await sendToLocalApp(jsonData, text);
  if (local?.ok) {{
    console.log(`%cCurso guardado en la app`, "color:#0f0;font-weight:bold");
    console.log(local.path);
    alert(
      `Listo: ${{ok}} videos en ${{jsonModules.length}} modulos\\n\\n` +
        `Guardado automaticamente en la app:\\n${{local.path}}\\n\\n` +
        `Vuelve a {APP_SCRIPT} y elige opcion 3.`
    );
    return jsonData;
  }}

  console.warn("Servidor local no disponible. Descargando ZIP como respaldo...");
  const zipName = `${{courseFolder}} - exportacion.zip`;
  try {{
    const JSZip = await loadJSZip();
    const zip = new JSZip();
    const root = zip.folder(courseFolder);
    root.file("videos.json", jsonText);
    root.file("videos.txt", text);
    root.file("LEEME.txt", readme);
    const blob = await zip.generateAsync({{ type: "blob", compression: "DEFLATE" }});
    await downloadBlob(zipName, blob);
    console.log(`ZIP descargado: ${{zipName}}`);
  }} catch (err) {{
    console.warn("ZIP no disponible, descargando archivos sueltos...", err);
    await downloadBlob(`${{courseFolder}}__videos.json`, new Blob([jsonText], {{ type: "application/json" }}));
    await downloadBlob(`${{courseFolder}}__videos.txt`, new Blob([text], {{ type: "text/plain" }}));
  }}

  console.log(`\\n=== ${{course.nombre.trim()}} ===`);
  console.log(`Modulos: ${{jsonModules.length}} | Videos: ${{ok}} | Fallidos: ${{failed.length}}`);
  alert(
    `Listo: ${{ok}} videos en ${{jsonModules.length}} modulos\\n\\n` +
      `App no detectada: descargado ${{zipName}}\\n\\n` +
      `Copialo a data/import/ o abre la app ANTES de ejecutar este script.`
  );
  return jsonData;
}})();
'''


def build_guide_text() -> str:
    return f"""{APP_NAME} v{APP_VERSION}
================================
{EXAMPLE_CASE_STUDY.strip()}

ESTRUCTURA (se crea sola al abrir la app):
  {APP_SCRIPT}                <- unico archivo que necesitas ejecutar
  data/
    cursos/                   <- biblioteca de cursos importados
    import/                   <- suelta aqui los ZIP del navegador
    session_token.txt         <- token opcional (plataforma de ejemplo)
    failed_videos.txt         <- videos fallidos para reintentar
  descargas/                  <- videos MP4 descargados
  assets/
    scripts/extract_catalog.js
    requirements.txt

EXTRAER CURSO EN EL NAVEGADOR (recomendado):
  1. Abre {APP_SCRIPT} y DEJALO ABIERTO
  2. Login en la plataforma de prueba -> catalogo del curso
  3. F12 -> Consola -> pega assets/scripts/extract_catalog.js
  4. El curso se guarda automaticamente en data/cursos/
  5. En la app elige opcion 3

Servidor local de importacion: http://{IMPORT_SERVER_HOST}:{IMPORT_SERVER_PORT}
(Solo activo mientras la app esta abierta)

Si la app no esta abierta, el script descarga un ZIP -> data/import/

RUTAS (relativas a la carpeta del script):
  App:     ./
  Cursos:  data/cursos/
  Import:  data/import/
  Salida:  descargas/

Las rutas completas se muestran en el panel al abrir la app.
"""


def write_app_assets() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    COURSES_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    REQUIREMENTS_FILE.write_text(EMBEDDED_REQUIREMENTS, encoding="utf-8")
    EXTRACT_SCRIPT.write_text(build_extract_catalog_js(), encoding="utf-8")
    GUIDE_FILE.write_text(build_guide_text(), encoding="utf-8")


def bootstrap_app() -> list[str]:
    write_app_assets()
    notes: list[str] = []
    notes.extend(migrate_legacy_layout())
    imported = import_zip_bundles()
    if imported:
        notes.extend([f"importado:{name}" for name in imported])
    return notes


def show_workspace_panel() -> None:
    courses = discover_courses()
    ffmpeg_ok = False
    try:
        find_ffmpeg()
        ffmpeg_ok = True
    except FileNotFoundError:
        pass

    if not ui_enabled():
        print(f"App: {APP_DIR}")
        print(f"Cursos: {len(courses)} | ffmpeg: {'OK' if ffmpeg_ok else 'NO'}")
        return

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Clave", style="muted")
    table.add_column("Valor", style="white")
    table.add_row("Version", APP_VERSION)
    table.add_row("Ubicacion", str(APP_DIR))
    table.add_row("Cursos listos", str(len(courses)))
    table.add_row("Biblioteca", str(COURSES_DIR))
    import_url = import_server_url()
    table.add_row(
        "Import navegador",
        f"[ok]{import_url}[/ok]" if import_url else "[warn]Inactivo[/warn]",
    )
    table.add_row("Import ZIP", str(IMPORT_DIR))
    table.add_row("Descargas", str(DOWNLOADS_DIR))
    table.add_row("ffmpeg", "[ok]Detectado[/ok]" if ffmpeg_ok else "[err]No encontrado[/err]")
    console.print(Panel(table, title="[accent]Espacio de trabajo[/accent]", border_style="blue"))


def pick_course_interactive() -> Path | None:
    courses = discover_courses()
    if not courses:
        ui_print(
            "\n[warn]No hay cursos en la biblioteca.[/warn]\n"
            "Abre la app, dejala abierta, extrae en el navegador (opcion 7)\n"
            "o copia un ZIP a:\n"
            f"  {IMPORT_DIR}\n",
            "warn",
        )
        return None

    if ui_enabled():
        table = Table(title="Biblioteca de cursos", box=box.ROUNDED, border_style="cyan")
        table.add_column("#", style="accent", justify="right", width=3)
        table.add_column("Curso", style="white")
        table.add_column("Videos", justify="right", style="accent")
        table.add_column("Modulos", justify="right", style="muted")
        for idx, course in enumerate(courses, start=1):
            table.add_row(str(idx), course["name"], str(course["videos"]), str(course["modules"]))
        console.print(table)
    else:
        print("\nCursos disponibles:")
        for idx, course in enumerate(courses, start=1):
            print(f"  {idx}. {course['name']} ({course['videos']} videos)")

    default = "1"
    raw = ask("Elige curso", default)
    try:
        index = int(raw) - 1
    except ValueError:
        index = 0
    index = max(0, min(index, len(courses) - 1))
    chosen = courses[index]
    ui_print(f"Curso seleccionado: [accent]{chosen['name']}[/accent]", "ok")
    return chosen["path"]


def open_path_in_explorer(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)



@dataclass
class VideoJob:
    video_id: str
    title: str | None = None
    folder: str | None = None
    course: str | None = None
    order: int | None = None
    cdn: str | None = None
    duration_seconds: float | None = None

    def sanitize(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]', "", value).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.rstrip(".")

    @property
    def safe_name(self) -> str:
        base = self.title or self.video_id
        name = self.sanitize(base)
        if self.order is not None:
            return f"{self.order:02d} - {name}"
        return name

    @property
    def safe_folder(self) -> str | None:
        if not self.folder:
            return None
        return self.sanitize(self.folder)

    @property
    def safe_course(self) -> str | None:
        if not self.course:
            return None
        return self.sanitize(self.course.strip())

    def output_path(self, base_dir: Path) -> Path:
        parts: list[Path] = [base_dir]
        if self.safe_course:
            parts.append(Path(self.safe_course))
        if self.safe_folder:
            parts.append(Path(self.safe_folder))
        target_dir = parts[0]
        for part in parts[1:]:
            target_dir = target_dir / part
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{self.safe_name}.mp4"
        path = target_dir / filename
        if path.exists() and path.stat().st_size > 0:
            return path

        stem = path.stem
        suffix = path.suffix
        n = 2
        while path.exists():
            path = target_dir / f"{stem} ({n}){suffix}"
            n += 1
        return path


@dataclass
class PreparedDownload:
    job: VideoJob
    out_file: Path
    rel: str
    skip: bool = False
    m3u8: str = ""
    used_quality: str = ""
    used_referer: str = ""
    duration: float = DEFAULT_VIDEO_DURATION
    note: str = ""


def ui_enabled() -> bool:
    return RICH_UI and console is not None


def ui_print(message: str = "", style: str | None = None) -> None:
    if ui_enabled() and console:
        console.print(message, style=style)
    else:
        plain = re.sub(r"\[[^\]]*\]", "", message)
        print(plain)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def short_label(text: str, max_len: int = 42) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def show_banner() -> None:
    if not ui_enabled():
        print("=" * 60)
        print(f"  {APP_NAME} v{APP_VERSION}")
        print("=" * 60)
        return
    console.print(
        Panel.fit(
            f"[banner]{APP_NAME}[/banner] [muted]v{APP_VERSION}[/muted]\n"
            "[muted]Estudio HLS · catalogo con login · caso de ejemplo[/muted]",
            border_style="magenta",
            box=box.DOUBLE,
        )
    )


def show_menu() -> None:
    options = [
        ("1", "Extraer videos desde URL del catalogo"),
        ("2", "Extraer videos desde catalogo.json"),
        ("3", "Descargar curso desde biblioteca (data/cursos)"),
        ("4", "Extraer + descargar (URL del catalogo)"),
        ("5", "Abrir carpeta de descargas"),
        ("6", "Plataforma con login (caso de estudio / token)"),
        ("7", "Guia: extraer curso en el navegador"),
        ("8", "Reintentar solo videos fallidos"),
        ("9", "Abrir carpetas del workspace (datos, import, scripts)"),
        ("0", "Salir"),
    ]

    if not ui_enabled():
        print()
        for key, label in options:
            print(f"  {key}) {label}")
        print()
        return

    table = Table(show_header=False, box=box.SIMPLE, padding=(0, 1))
    table.add_column("Opcion", style="accent", width=4)
    table.add_column("Descripcion", style="menu")
    for key, label in options:
        table.add_row(key, label)
    console.print(Panel(table, title="[accent]Menu principal[/accent]", border_style="cyan"))


def validate_jobs(jobs: list[VideoJob]) -> None:
    modules: dict[str, int] = {}
    for job in jobs:
        key = job.folder or "(sin modulo)"
        modules[key] = modules.get(key, 0) + 1

    if ui_enabled():
        table = Table(title="Lista cargada", box=box.ROUNDED, border_style="cyan")
        table.add_column("Modulo", style="white")
        table.add_column("Videos", justify="right", style="accent")
        for folder, count in modules.items():
            table.add_row(folder, str(count))
        table.add_row("[bold]TOTAL[/bold]", f"[bold]{len(jobs)}[/bold]")
        console.print(table)
    else:
        print(f"Cargados {len(jobs)} videos")
        for folder, count in modules.items():
            print(f"  - {folder}: {count} videos")


def format_module_folder(orden: int | float | None, nombre: str | None) -> str:
    name = (nombre or "Sin nombre").strip()
    if orden is None:
        return name
    num = int(orden)
    prefix = str(num) if num < 0 else f"{num:02d}"
    return f"{prefix} - {name}"


def extract_cdn_from_embed(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(vz-[a-z0-9-]+\.b-cdn\.net)", value, re.I)
    return match.group(1).lower() if match else None


def bootstrap_rich_ui() -> None:
    global RICH_UI, console, APP_THEME
    if RICH_UI:
        return
    try:
        from rich.console import Console
        from rich.theme import Theme

        APP_THEME = Theme(
            {
                "banner": "bold magenta",
                "accent": "bold cyan",
                "ok": "bold green",
                "warn": "bold yellow",
                "err": "bold red",
                "muted": "dim white",
                "menu": "white",
            }
        )
        RICH_UI = True
    except ImportError:
        RICH_UI = False


def ensure_requirements() -> None:
    write_app_assets()
    packages = [
        line.strip()
        for line in EMBEDDED_REQUIREMENTS.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not packages:
        return

    ui_print("Preparando entorno...", "accent")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", *packages, "-q"],
        check=False,
    )
    ui_print("Entorno listo.\n", "ok")
    bootstrap_rich_ui()


def pause_if_interactive() -> None:
    if len(sys.argv) == 1 and sys.platform == "win32":
        try:
            input("\nPulsa Enter para cerrar...")
        except EOFError:
            pass


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_int(prompt: str, default: int, minimum: int = 1, maximum: int = 5) -> int:
    raw = ask(prompt, str(default))
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    for candidate in (
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
    ):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "No se encontro ffmpeg.\n"
        "Instalalo con: choco install ffmpeg -y\n"
        "O con: winget install Gyan.FFmpeg"
    )


def try_install_ffmpeg() -> bool:
    print("\nIntentando instalar ffmpeg automaticamente...")
    if shutil.which("choco"):
        result = subprocess.run(["choco", "install", "ffmpeg", "-y"], text=True)
        if result.returncode == 0:
            return True
    if shutil.which("winget"):
        result = subprocess.run(
            [
                "winget",
                "install",
                "--id",
                "Gyan.FFmpeg",
                "-e",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            text=True,
        )
        if result.returncode == 0:
            return True
    return False


def extract_uuid(line: str) -> str | None:
    match = UUID_RE.search(line.strip())
    return match.group(0).lower() if match else None


def parse_line(line: str, current_course: str | None = None) -> VideoJob | None | str:
    line = line.strip()
    if not line:
        return None
    if line.startswith("#"):
        if line.upper().startswith("# CURSO:"):
            return line.split(":", 1)[1].strip()
        return None

    video_id = extract_uuid(line)
    if not video_id:
        return None

    title = None
    folder = None
    parts = [p.strip() for p in line.split("|")]

    if len(parts) >= 3 and extract_uuid(parts[-1]):
        folder = parts[0] or None
        title = parts[1] or None
        video_id = extract_uuid(parts[-1]) or video_id
    elif len(parts) == 2 and extract_uuid(parts[1]):
        left = parts[0]
        maybe_id = extract_uuid(parts[1])
        if maybe_id:
            video_id = maybe_id
            if left and left != video_id:
                title = left

    return VideoJob(
        video_id=video_id,
        title=title,
        folder=folder,
        course=current_course,
    )


def load_jobs_from_file(path: Path) -> list[VideoJob]:
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo: {path}")

    if path.suffix.lower() == ".json":
        return load_jobs_from_videos_json(path)

    jobs: list[VideoJob] = []
    seen: set[str] = set()
    current_course: str | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_line(line, current_course)
        if isinstance(parsed, str):
            current_course = parsed
            continue
        if parsed and parsed.video_id not in seen:
            seen.add(parsed.video_id)
            jobs.append(parsed)
    return jobs


def load_jobs_from_videos_json(path: Path) -> list[VideoJob]:
    data = json.loads(path.read_text(encoding="utf-8"))
    course = (data.get("course") or data.get("curso") or "").strip() or None
    default_cdn = data.get("default_cdn") or DEFAULT_CDN
    jobs: list[VideoJob] = []
    seen: set[str] = set()

    modules = data.get("modules") or data.get("modulos") or []
    for module in modules:
        mod_orden = module.get("orden")
        mod_nombre = module.get("nombre") or module.get("folder")
        folder = module.get("folder") or format_module_folder(mod_orden, mod_nombre)
        lessons = module.get("lessons") or module.get("lecciones") or []
        lessons = sorted(
            lessons,
            key=lambda lesson: (
                lesson.get("order_in_module")
                or lesson.get("orden")
                or 0,
            ),
        )

        for idx, lesson in enumerate(lessons, start=1):
            video_id = lesson.get("video_id") or uuid_from_embed(
                lesson.get("embed_url")
                or lesson.get("video_embed_url")
                or lesson.get("embed")
            )
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            order = lesson.get("order_in_module") or idx
            cdn = lesson.get("cdn") or extract_cdn_from_embed(
                lesson.get("embed_url") or lesson.get("video_embed_url")
            ) or default_cdn
            duration_raw = lesson.get("duracion_segundos") or lesson.get("duration_seconds")
            duration = float(duration_raw) if duration_raw else None
            jobs.append(
                VideoJob(
                    video_id=video_id,
                    title=(lesson.get("title") or lesson.get("nombre") or "").strip(),
                    folder=folder,
                    course=course,
                    order=int(order),
                    cdn=cdn,
                    duration_seconds=duration,
                )
            )
    return jobs


def load_session_token(path: Path | str | None = None) -> str | None:
    token_file = Path(path) if path else DEFAULT_TOKEN
    if not token_file.exists():
        return None
    for line in token_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def slug_from_catalog_url(url: str) -> str | None:
    parts = [p for p in url.rstrip("/").split("/") if p]
    if "catalog" in parts:
        idx = parts.index("catalog")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1] if parts else None


def supabase_request(path: str, token: str, method: str = "GET", body: dict | None = None) -> object:
    url = f"{PLATFORM_SUPABASE_URL}{path}"
    headers = {
        "apikey": PLATFORM_API_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"Supabase HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Error de red Supabase: {exc}") from exc


def uuid_from_embed(value: str | None) -> str | None:
    if not value:
        return None
    match = UUID_RE.search(value)
    return match.group(0).lower() if match else None


def fetch_platform_course(slug: str, token: str) -> list[VideoJob]:
    courses = supabase_request(
        f"/rest/v1/course_summaries?slug=eq.{urllib.parse.quote(slug)}&select=id,slug,nombre",
        token,
    )
    if not courses:
        raise RuntimeError(f"Curso no encontrado para slug: {slug}")

    course = courses[0]
    course_name = course.get("nombre") or slug
    modules = supabase_request(
        "/rest/v1/modules?"
        f"curso_id=eq.{course['id']}"
        "&select=id,nombre,orden,lessons(id,nombre,orden,video_embed_url)"
        "&order=orden.asc",
        token,
    )

    jobs: list[VideoJob] = []
    seen: set[str] = set()

    for module in sorted(modules or [], key=lambda m: m.get("orden", 0)):
        mod_order = module.get("orden")
        mod_name = module.get("nombre") or "Sin nombre"
        folder = format_module_folder(mod_order, mod_name)
        lessons = sorted(module.get("lessons") or [], key=lambda l: l.get("orden", 0))

        for idx, lesson in enumerate(lessons, start=1):
            embed = lesson.get("video_embed_url")
            video_id = uuid_from_embed(embed)
            cdn = extract_cdn_from_embed(embed) or DEFAULT_CDN

            if not video_id:
                try:
                    payload = supabase_request(
                        "/functions/v1/get-video-embed",
                        token,
                        method="POST",
                        body={"lessonId": lesson["id"]},
                    )
                    if isinstance(payload, dict):
                        embed = payload.get("embedUrl") or payload.get("embed_url")
                        video_id = uuid_from_embed(embed)
                        cdn = extract_cdn_from_embed(embed) or DEFAULT_CDN
                except RuntimeError:
                    video_id = None

            if video_id and video_id not in seen:
                seen.add(video_id)
                jobs.append(
                    VideoJob(
                        video_id=video_id,
                        title=(lesson.get("nombre") or "").strip(),
                        folder=folder,
                        course=course_name.strip(),
                        order=idx,
                        cdn=cdn,
                    )
                )

    return jobs


def fetch_url_text(url: str, referer: str, cookie: str | None = None) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": referer,
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"No se pudo leer: {exc}") from exc


def jobs_from_json_text(text: str, seen: set[str] | None = None) -> list[VideoJob]:
    if seen is None:
        seen = set()
    jobs: list[VideoJob] = []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return jobs

    def walk(node: object, title_hint: str | None = None) -> None:
        if isinstance(node, dict):
            title = title_hint
            for key in ("title", "name", "label", "nombre"):
                val = node.get(key)
                if isinstance(val, str) and val.strip():
                    title = val.strip()
                    break

            for key in ("videoId", "video_id", "guid", "bunnyVideoId", "bunny_video_id"):
                val = node.get(key)
                if isinstance(val, str):
                    vid = extract_uuid(val)
                    if vid and vid not in seen:
                        seen.add(vid)
                        jobs.append(VideoJob(video_id=vid, title=title))

            for key in ("video_embed_url", "embedUrl", "embed_url"):
                val = node.get(key)
                if isinstance(val, str):
                    vid = uuid_from_embed(val)
                    if vid and vid not in seen:
                        seen.add(vid)
                        jobs.append(VideoJob(video_id=vid, title=title))

            for val in node.values():
                walk(val, title)
        elif isinstance(node, list):
            for item in node:
                walk(item, title_hint)

    walk(data)
    return jobs


def jobs_from_text(text: str) -> list[VideoJob]:
    seen: set[str] = set()
    jobs: list[VideoJob] = []

    for match in UUID_RE.finditer(text):
        video_id = match.group(0).lower()
        if video_id not in seen:
            seen.add(video_id)
            jobs.append(VideoJob(video_id=video_id))

    for script_match in re.finditer(
        r"<script[^>]*type=[\"']application/json[\"'][^>]*>(.*?)</script>",
        text,
        re.DOTALL | re.IGNORECASE,
    ):
        jobs.extend(jobs_from_json_text(script_match.group(1), seen))

    return jobs


def fetch_page_uuids(url: str, referer: str, cookie: str | None = None) -> list[VideoJob]:
    html = fetch_url_text(url, referer, cookie)
    jobs = jobs_from_text(html)
    if not jobs and ('id="root"' in html or "createRoot" in html or "/assets/index-" in html):
        raise RuntimeError(LOGIN_REQUIRED_HINT.strip())
    return jobs


def load_jobs_from_json(path: Path) -> list[VideoJob]:
    if not path.exists():
        raise FileNotFoundError(f"No existe el archivo: {path}")
    return jobs_from_json_text(path.read_text(encoding="utf-8"))


def dedupe_jobs(jobs: list[VideoJob]) -> list[VideoJob]:
    seen: set[str] = set()
    unique: list[VideoJob] = []
    for job in jobs:
        if job.video_id not in seen:
            seen.add(job.video_id)
            unique.append(job)
    return unique


def build_m3u8_url(video_id: str, cdn: str, quality: str) -> str:
    return f"https://{cdn}/{video_id}/{quality}/video.m3u8"


def build_master_url(video_id: str, cdn: str) -> str:
    return f"https://{cdn}/{video_id}/playlist.m3u8"


def referer_candidates(primary: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for ref in (primary, *FALLBACK_REFERERS):
        ref = ref.strip()
        if ref and ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def url_exists(url: str, referer: str) -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": referer,
        "Range": "bytes=0-0",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status in (200, 206)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 206)
    except urllib.error.URLError:
        return False


def fetch_master_qualities(video_id: str, cdn: str, referer: str) -> list[str]:
    master_url = build_master_url(video_id, cdn)
    if not url_exists(master_url, referer):
        return []

    req = urllib.request.Request(
        master_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": referer,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.URLError:
        return []

    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.endswith("/video.m3u8"):
            quality = line.split("/")[0]
            if quality in QUALITIES and quality not in found:
                found.append(quality)
    return found


def pick_quality(available: list[str], preferred: str) -> str | None:
    if not available:
        return None

    rank = {quality: index for index, quality in enumerate(QUALITIES)}
    available_sorted = sorted(available, key=lambda q: rank.get(q, 99))

    if preferred in ("best", "auto"):
        return available_sorted[0]
    if preferred in available:
        return preferred

    preferred_rank = rank.get(preferred, 0)
    for quality in available_sorted:
        if rank.get(quality, 99) >= preferred_rank:
            return quality
    return available_sorted[0]


def resolve_m3u8_url(
    video_id: str,
    cdn: str,
    referer: str,
    preferred: str,
) -> tuple[str, str, str]:
    cache_key = (video_id, preferred, cdn)
    with _M3U8_CACHE_LOCK:
        cached = _M3U8_CACHE.get(cache_key)
    if cached:
        return cached

    last_error = "no se encontro ninguna calidad disponible"

    for ref in referer_candidates(referer):
        available = fetch_master_qualities(video_id, cdn, ref)
        quality = pick_quality(available, preferred)
        if quality:
            resolved = (build_m3u8_url(video_id, cdn, quality), quality, ref)
            with _M3U8_CACHE_LOCK:
                _M3U8_CACHE[cache_key] = resolved
            return resolved

        qualities_to_try: list[str]
        if preferred in ("best", "auto"):
            qualities_to_try = list(QUALITIES)
        elif preferred in QUALITIES:
            start = QUALITIES.index(preferred)
            qualities_to_try = list(QUALITIES[start:])
        else:
            qualities_to_try = list(QUALITIES)

        for quality in qualities_to_try:
            url = build_m3u8_url(video_id, cdn, quality)
            if url_exists(url, ref):
                resolved = (url, quality, ref)
                with _M3U8_CACHE_LOCK:
                    _M3U8_CACHE[cache_key] = resolved
                return resolved

        last_error = f"sin acceso con referer {ref}"

    raise RuntimeError(last_error)


def probe_duration_from_m3u8(m3u8_url: str, referer: str) -> float:
    req = urllib.request.Request(
        m3u8_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": referer,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.URLError:
        return DEFAULT_VIDEO_DURATION

    total = 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                total += float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                continue
    return total if total > 1 else DEFAULT_VIDEO_DURATION


def prepare_download_item(
    job: VideoJob,
    output_dir: Path,
    cdn: str,
    quality: str,
    referer: str,
) -> PreparedDownload:
    job_cdn = job.cdn or cdn
    out_file = job.output_path(output_dir)
    rel = str(out_file.relative_to(output_dir))

    if out_file.exists() and out_file.stat().st_size > 0:
        return PreparedDownload(
            job=job,
            out_file=out_file,
            rel=rel,
            skip=True,
            note="ya existe",
        )

    try:
        m3u8, used_quality, used_referer = resolve_m3u8_url(
            job.video_id, job_cdn, referer, quality
        )
    except RuntimeError as exc:
        return PreparedDownload(
            job=job,
            out_file=out_file,
            rel=rel,
            skip=True,
            note=f"error: {exc}",
        )

    duration = job.duration_seconds or probe_duration_from_m3u8(m3u8, used_referer)
    return PreparedDownload(
        job=job,
        out_file=out_file,
        rel=rel,
        m3u8=m3u8,
        used_quality=used_quality,
        used_referer=used_referer,
        duration=duration,
    )


def prepare_all_downloads(
    jobs: list[VideoJob],
    output_dir: Path,
    cdn: str,
    quality: str,
    referer: str,
) -> list[PreparedDownload]:
    prepared: list[PreparedDownload] = []

    def _prepare(job: VideoJob) -> PreparedDownload:
        return prepare_download_item(job, output_dir, cdn, quality, referer)

    if ui_enabled():
        with Progress(
            TextColumn("[accent]Preparando[/accent] {task.description}"),
            BarColumn(bar_width=30, complete_style="green"),
            MofNCompleteColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task("videos", total=len(jobs))
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(_prepare, job) for job in jobs]
                for future in as_completed(futures):
                    prepared.append(future.result())
                    progress.advance(task_id)
    else:
        with ThreadPoolExecutor(max_workers=6) as pool:
            prepared = list(pool.map(_prepare, jobs))

    prepared.sort(key=lambda item: (item.job.folder or "", item.job.order or 0, item.job.title or ""))
    return prepared


def prepare_error_items(prepared: list[PreparedDownload]) -> list[PreparedDownload]:
    return [item for item in prepared if item.skip and item.note.startswith("error:")]


def show_download_plan(prepared: list[PreparedDownload], output_dir: Path, quality: str) -> None:
    pending = [item for item in prepared if not item.skip or item.note == "ya existe"]
    to_download = [item for item in prepared if not item.skip]
    skipped = [item for item in prepared if item.skip and item.note == "ya existe"]
    prepare_errors = prepare_error_items(prepared)
    total_duration = sum(item.duration for item in to_download)
    est_download = total_duration * DOWNLOAD_SPEED_FACTOR

    if ui_enabled():
        table = Table(title="Plan de descarga", box=box.ROUNDED, border_style="magenta")
        table.add_column("Concepto", style="white")
        table.add_column("Valor", style="accent", justify="right")
        table.add_row("Videos totales", str(len(prepared)))
        table.add_row("Por descargar", str(len(to_download)))
        table.add_row("Ya descargados (skip)", str(len(skipped)))
        if prepare_errors:
            table.add_row("Errores al preparar", str(len(prepare_errors)))
        table.add_row("Duracion total del contenido", format_duration(total_duration))
        table.add_row("Tiempo estimado de descarga", f"~{format_duration(est_download)}")
        table.add_row("Calidad pedida", quality)
        table.add_row("Carpeta destino", str(output_dir.resolve()))
        console.print(table)
        if prepare_errors:
            console.print(
                "[warn]Algunos videos no se pudieron preparar (URL/calidad). "
                "Se listaran como fallidos al iniciar la descarga.[/warn]"
            )
        if to_download:
            console.print(
                "[muted]La barra de cada video muestra progreso y tiempo restante aproximado.[/muted]\n"
            )
    else:
        print(f"\nVideos: {len(prepared)} | Descargar: {len(to_download)} | Skip: {len(skipped)}")
        print(f"Duracion contenido: {format_duration(total_duration)}")
        print(f"Tiempo estimado: ~{format_duration(est_download)}")
        print(f"Salida: {output_dir.resolve()}\n")


def run_ffmpeg_with_progress(
    ffmpeg: str,
    item: PreparedDownload,
    progress: Progress | None,
    task_id: int | None,
) -> tuple[bool, str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-headers",
        f"Referer: {item.used_referer}\r\n",
        "-i",
        item.m3u8,
        "-c",
        "copy",
        "-y",
        str(item.out_file),
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert proc.stdout is not None
    duration_us = max(int(item.duration * 1_000_000), 1)
    last_pct = 0.0

    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("out_time_us="):
            continue
        try:
            out_us = int(line.split("=", 1)[1])
        except ValueError:
            continue
        pct = min(out_us / duration_us, 1.0)
        if progress and task_id is not None and pct - last_pct >= 0.01:
            progress.update(task_id, completed=pct * item.duration)
            last_pct = pct

    stderr = proc.stderr.read() if proc.stderr else ""
    code = proc.wait()

    if progress and task_id is not None:
        progress.update(task_id, completed=item.duration)

    if code == 0:
        return True, item.used_quality
    detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"codigo {code}"
    return False, detail[:300]


def save_jobs_list(jobs: list[VideoJob], path: Path, cdn: str, quality: str) -> None:
    lines: list[str] = []
    course = next((job.course for job in jobs if job.course), None)
    if course:
        lines.append(f"# CURSO: {course}")

    for job in jobs:
        if job.folder and job.title:
            lines.append(f"{job.folder} | {job.title} | {job.video_id}")
        elif job.title:
            lines.append(f"{job.title} | {job.video_id}")
        else:
            lines.append(job.video_id)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    json_path = path.with_suffix(".json")
    modules: dict[str, list[dict]] = {}
    for job in jobs:
        key = job.folder or "Sin modulo"
        modules.setdefault(key, []).append(
            {
                "title": job.title,
                "nombre": job.title,
                "video_id": job.video_id,
                "order_in_module": job.order,
                "orden": job.order,
                "cdn": job.cdn or DEFAULT_CDN,
            }
        )
    json_path.write_text(
        json.dumps(
            {
                "version": 2,
                "course": course.strip() if course else course,
                "curso": course.strip() if course else course,
                "default_cdn": DEFAULT_CDN,
                "modules": [
                    {"folder": folder, "nombre": folder, "lessons": lessons}
                    for folder, lessons in modules.items()
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    m3u8_path = path.with_name(path.stem + "_m3u8.txt")
    m3u8_lines = [build_m3u8_url(job.video_id, job.cdn or cdn, quality) for job in jobs]
    m3u8_path.write_text("\n".join(m3u8_lines) + "\n", encoding="utf-8")


def collect_jobs(args: argparse.Namespace) -> list[VideoJob]:
    jobs: list[VideoJob] = []
    cookie = getattr(args, "cookie", None) or load_session_token(getattr(args, "cookie_file", None))

    course_slug = getattr(args, "course_slug", None) or getattr(args, "robinhood_slug", None)
    if course_slug:
        token = cookie or load_session_token()
        if not token:
            raise RuntimeError(
                "Falta session_token.txt con tu access_token de la plataforma.\n"
                + LOGIN_REQUIRED_HINT.strip()
            )
        print(f"Extrayendo curso (caso de estudio): {course_slug}")
        jobs.extend(fetch_platform_course(course_slug, token))
    if args.from_page:
        if "/catalog/" in args.from_page:
            slug = slug_from_catalog_url(args.from_page)
            token = cookie or load_session_token()
            if slug and token:
                print(f"Detectado catalogo con sesion -> slug: {slug}")
                jobs.extend(fetch_platform_course(slug, token))
            else:
                raise RuntimeError(LOGIN_REQUIRED_HINT.strip())
        else:
            print(f"Extrayendo desde pagina: {args.from_page}")
            jobs.extend(fetch_page_uuids(args.from_page, args.referer, cookie))
    if args.from_json:
        print(f"Leyendo JSON: {args.from_json}")
        jobs.extend(load_jobs_from_json(Path(args.from_json)))
    if args.list_file:
        print(f"Leyendo lista: {args.list_file}")
        jobs.extend(load_jobs_from_file(Path(args.list_file)))
    return dedupe_jobs(jobs)


def run_extract(args: argparse.Namespace, jobs: list[VideoJob]) -> int:
    course = next((job.course for job in jobs if job.course), None)
    slug = getattr(args, "course_slug", None) or getattr(args, "robinhood_slug", None)
    if course or slug:
        bundle_dir = COURSES_DIR / sanitize_dir_name(course or slug or "curso")
    else:
        bundle_dir = Path(args.save_list).parent
    bundle_dir.mkdir(parents=True, exist_ok=True)
    out_list = bundle_dir / "videos.txt"
    save_jobs_list(jobs, out_list, args.cdn, args.quality)
    ui_print(f"\n[ok]Extraidos {len(jobs)} videos[/ok]", "ok")
    ui_print(f"  Carpeta:  {bundle_dir.resolve()}", "muted")
    ui_print(f"  Lista:    {out_list.resolve()}", "muted")
    ui_print(f"  JSON:     {out_list.with_suffix('.json').resolve()}", "muted")
    return 0


def run_download(args: argparse.Namespace, jobs: list[VideoJob]) -> int:
    validate_jobs(jobs)
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError as exc:
        ui_print(str(exc), "err")
        if len(sys.argv) == 1 and ask("Quieres intentar instalar ffmpeg ahora? (s/n)", "s").lower().startswith("s"):
            if try_install_ffmpeg():
                ffmpeg = find_ffmpeg()
            else:
                ui_print("Instala ffmpeg manualmente y vuelve a ejecutar.", "warn")
                return 1
        else:
            return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    preferred = args.quality

    prepared = prepare_all_downloads(jobs, output_dir, args.cdn, preferred, args.referer)
    show_download_plan(prepared, output_dir, preferred)

    ok, fail, failed_jobs = _download_batch_ui(
        ffmpeg, prepared, preferred, args.workers, output_dir
    )

    if failed_jobs:
        ui_print(f"\nReintentando {len(failed_jobs)} videos fallidos...", "warn")
        retry_prepared = prepare_all_downloads(
            failed_jobs, output_dir, args.cdn, preferred, args.referer
        )
        retry_ok, retry_fail, still_failed = _download_batch_ui(
            ffmpeg,
            retry_prepared,
            preferred,
            max(1, min(args.workers, 2)),
            output_dir,
        )
        ok += retry_ok
        fail = retry_fail
        failed_jobs = still_failed

    if failed_jobs:
        failed_path = FAILED_VIDEOS
        lines = []
        for job in failed_jobs:
            label = job.title or job.video_id
            if job.folder:
                lines.append(f"{job.folder} | {label} | {job.video_id}")
            else:
                lines.append(f"{label} | {job.video_id}")
        failed_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ui_print(f"Lista de fallidos: {failed_path.resolve()}", "warn")

    if ui_enabled():
        summary = Table(box=box.ROUNDED, border_style="green" if fail == 0 else "yellow")
        summary.add_column("Resultado", style="white")
        summary.add_column("Cantidad", justify="right", style="accent")
        summary.add_row("Correctos", str(ok))
        summary.add_row("Fallidos", str(fail))
        console.print(Panel(summary, title="[ok]Descarga finalizada[/ok]" if fail == 0 else "[warn]Descarga completada con errores[/warn]"))
    else:
        print(f"Listo: {ok} ok, {fail} fallidos")

    return 0 if fail == 0 else 2


def _download_batch_ui(
    ffmpeg: str,
    prepared: list[PreparedDownload],
    quality: str,
    workers: int,
    output_dir: Path,
) -> tuple[int, int, list[VideoJob]]:
    ok = 0
    fail = 0
    failed_jobs: list[VideoJob] = []
    workers = 1 if ui_enabled() else max(1, min(workers, 5))

    downloadable = [item for item in prepared if not item.skip]
    skipped = [item for item in prepared if item.skip and item.note == "ya existe"]
    prepare_errors = prepare_error_items(prepared)
    ok += len(skipped)

    for item in prepare_errors:
        fail += 1
        failed_jobs.append(item.job)
        ui_print(f"[err]FAIL[/err] {item.rel}: {item.note}", "err")

    if not downloadable and (skipped or prepare_errors):
        ui_print(f"Todos los videos ya estaban descargados ({len(skipped)}).", "ok")
        return ok, fail, failed_jobs

    if not ui_enabled():
        return _download_batch_plain(ffmpeg, prepared, quality, workers, ok)

    with Progress(
        TextColumn("{task.description}"),
        BarColumn(
            bar_width=40,
            complete_style="green",
            finished_style="bright_green",
            pulse_style="cyan",
        ),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        TimeElapsedColumn(),
        console=console,
        expand=False,
    ) as progress:
        overall = progress.add_task(
            "[accent]Progreso total[/accent]",
            total=len(downloadable),
        )

        for item in downloadable:
            label = short_label(item.job.title or item.job.video_id)
            task_id = progress.add_task(
                f"[white]{label}[/white]",
                total=max(item.duration, 1.0),
            )

            success = False
            note = item.note
            current = item

            try:
                for attempt in range(1, 4):
                    if attempt > 1:
                        job_cdn = current.job.cdn or DEFAULT_CDN
                        with _M3U8_CACHE_LOCK:
                            _M3U8_CACHE.pop((current.job.video_id, quality, job_cdn), None)
                        refreshed = prepare_download_item(
                            current.job,
                            output_dir,
                            job_cdn,
                            "best",
                            current.used_referer or DEFAULT_REFERER,
                        )
                        if refreshed.skip and refreshed.note != "ya existe":
                            note = refreshed.note
                            break
                        current = refreshed
                        progress.update(task_id, total=max(current.duration, 1.0), completed=0)

                    success, note = run_ffmpeg_with_progress(ffmpeg, current, progress, task_id)
                    if success:
                        break
                    time.sleep(attempt)
            except Exception as exc:
                success = False
                note = str(exc)

            progress.remove_task(task_id)
            progress.advance(overall)

            if success:
                ok += 1
            else:
                fail += 1
                failed_jobs.append(current.job)
                ui_print(f"[err]FAIL[/err] {current.rel}: {note}", "err")

    return ok, fail, failed_jobs


def _download_batch_plain(
    ffmpeg: str,
    prepared: list[PreparedDownload],
    quality: str,
    workers: int,
    initial_ok: int,
) -> tuple[int, int, list[VideoJob]]:
    ok = initial_ok
    fail = 0
    failed_jobs: list[VideoJob] = []

    def work(item: PreparedDownload) -> tuple[PreparedDownload, bool, str]:
        if item.skip:
            return item, item.note == "ya existe", item.note
        success, note = run_ffmpeg_with_progress(ffmpeg, item, None, None)
        return item, success, note

    items = [item for item in prepared if not item.skip]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, item): item for item in items}
        total = len(futures)
        for i, future in enumerate(as_completed(futures), 1):
            item, success, note = future.result()
            status = "OK" if success else "FAIL"
            print(f"[{i}/{total}] {status} {item.rel} [{note}]")
            if success:
                ok += 1
            else:
                fail += 1
                failed_jobs.append(item.job)

    return ok, fail, failed_jobs


def run_pipeline(args: argparse.Namespace) -> int:
    try:
        jobs = collect_jobs(args)
    except (FileNotFoundError, RuntimeError) as exc:
        ui_print(f"ERROR: {exc}", "err")
        return 1

    if not jobs:
        ui_print("No se encontraron videos.", "warn")
        ui_print(LOGIN_REQUIRED_HINT, "muted")
        return 1

    if args.extract_only:
        return run_extract(args, jobs)

    try:
        return run_download(args, jobs)
    except Exception as exc:
        ui_print(f"\n[err]Error durante la descarga:[/err] {exc}", "err")
        if ui_enabled() and console:
            console.print_exception(show_locals=False)
        else:
            import traceback

            traceback.print_exc()
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} — descarga de cursos HLS")
    parser.add_argument("list_file", nargs="?", help="Archivo .txt con UUIDs o URLs")
    parser.add_argument("--from-page", help="URL del catalogo/handler para extraer UUIDs")
    parser.add_argument("--from-json", help="Respuesta JSON copiada de Red -> Fetch/XHR")
    parser.add_argument(
        "--course-slug",
        dest="course_slug",
        help="Slug del curso en plataforma de ejemplo (patron /catalog/{slug})",
    )
    parser.add_argument(
        "--robinhood-slug",
        dest="course_slug",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--cookie-file", default=str(DEFAULT_TOKEN), help="Archivo con access_token o Cookie")
    parser.add_argument("--extract-only", action="store_true", help="Solo extrae, no descarga")
    parser.add_argument("--save-list", default=str(DEFAULT_LIST), help="Archivo de lista extraida")
    parser.add_argument("--output", "-o", default=str(DEFAULT_OUTPUT), help="Carpeta de salida")
    parser.add_argument("--quality", "-q", default="best", choices=QUALITY_MODES)
    parser.add_argument("--cdn", default=DEFAULT_CDN, help="Host CDN Bunny")
    parser.add_argument("--referer", default=DEFAULT_REFERER, help="Header Referer")
    parser.add_argument("--workers", "-w", type=int, default=2, help="Descargas paralelas")
    return parser


def interactive_menu() -> int:
    os.chdir(APP_DIR)
    bootstrap_notes = bootstrap_app()
    show_banner()
    show_workspace_panel()
    if bootstrap_notes:
        ui_print(f"[muted]Migracion/importacion: {len(bootstrap_notes)} cambio(s)[/muted]\n", "muted")
    show_menu()

    valid_choices = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}
    while True:
        choice = ask("Elige una opcion").strip()
        if choice in valid_choices:
            break
        ui_print("Opcion no valida. Introduce un numero del menu (0-9).", "warn")

    if choice == "0":
        return 0

    quality = ask("Calidad (best = max disponible)", "best")
    if quality not in QUALITY_MODES:
        quality = "best"

    referer = ask("Referer (Enter = default Bunny)", DEFAULT_REFERER)
    workers = ask_int("Descargas en paralelo", 2)

    args = argparse.Namespace(
        list_file=None,
        from_page=None,
        from_json=None,
        course_slug=None,
        cookie_file=str(DEFAULT_TOKEN),
        cookie=None,
        extract_only=False,
        save_list=str(DEFAULT_LIST),
        output=str(DEFAULT_OUTPUT),
        quality=quality,
        cdn=DEFAULT_CDN,
        referer=referer,
        workers=workers,
    )

    if choice == "5":
        open_path_in_explorer(DOWNLOADS_DIR)
        return 0

    if choice == "9":
        ui_print("\n[accent]Carpetas del workspace[/accent]", "accent")
        ui_print(f"  Datos:   {DATA_DIR}", "muted")
        ui_print(f"  Import:  {IMPORT_DIR}", "muted")
        ui_print(f"  Scripts: {SCRIPTS_DIR}", "muted")
        ui_print(f"  Guia:    {GUIDE_FILE}", "muted")
        target = ask("Abrir (datos/import/scripts/guia)", "import").lower()
        if target.startswith("s"):
            open_path_in_explorer(SCRIPTS_DIR)
        elif target.startswith("g"):
            open_path_in_explorer(ASSETS_DIR)
            if sys.platform == "win32" and GUIDE_FILE.exists():
                os.startfile(GUIDE_FILE)  # type: ignore[attr-defined]
        elif target.startswith("d"):
            open_path_in_explorer(DATA_DIR)
        else:
            open_path_in_explorer(IMPORT_DIR)
        return 0

    if choice == "7":
        ui_print(EXAMPLE_CASE_STUDY, "muted")
        ui_print(LOGIN_REQUIRED_HINT, "muted")
        import_url = import_server_url()
        if import_url:
            ui_print(f"\n[ok]Servidor local activo:[/ok] {import_url}", "ok")
        else:
            ui_print("\n[warn]Servidor local inactivo.[/warn] Deja esta ventana abierta.", "warn")
        ui_print(f"\n[accent]Script del navegador:[/accent]\n  {EXTRACT_SCRIPT}", "accent")
        ui_print(f"\n[muted]Guia completa:[/muted] {GUIDE_FILE}", "muted")
        if ask("Abrir script en el bloc de notas? (s/n)", "s").lower().startswith("s"):
            if sys.platform == "win32":
                os.startfile(EXTRACT_SCRIPT)  # type: ignore[attr-defined]
        return 0

    if choice == "8":
        if not FAILED_VIDEOS.exists():
            ui_print(f"No existe {FAILED_VIDEOS}. Primero haz una descarga normal.", "warn")
            return 1
        args.list_file = str(FAILED_VIDEOS)
        return run_pipeline(args)

    if choice == "6":
        catalog_url = ask(
            "URL del catalogo (plataforma de ejemplo)",
            "",
        )
        slug = slug_from_catalog_url(catalog_url) or ask("Slug del curso", "")
        if not load_session_token():
            ui_print(f"\nCrea el archivo: {DEFAULT_TOKEN}", "warn")
            ui_print("Pega tu access_token (F12 -> Application -> Local Storage -> auth-token)", "muted")
            token = ask("O pega aqui el access_token ahora")
            if token:
                DEFAULT_TOKEN.parent.mkdir(parents=True, exist_ok=True)
                DEFAULT_TOKEN.write_text(token.strip() + "\n", encoding="utf-8")
            else:
                ui_print("Sin token no se puede extraer. Usa la opcion 7 (navegador).", "warn")
                return 1
        mode = ask("Solo extraer (e) o extraer y descargar (d)", "d").lower()
        args.course_slug = slug
        if mode.startswith("e"):
            args.extract_only = True
            return run_pipeline(args)
        args.extract_only = True
        code = run_pipeline(args)
        if code != 0:
            return code
        args.extract_only = False
        args.course_slug = None
        course_path = pick_course_interactive()
        if not course_path:
            return 1
        args.list_file = str(course_path)
        return run_pipeline(args)

    if choice == "1":
        args.from_page = ask("URL del catalogo")
        args.extract_only = True
    elif choice == "2":
        json_path = ask("Ruta a catalogo.json", str(DATA_DIR / "catalogo.json"))
        args.from_json = json_path
        args.extract_only = True
    elif choice == "3":
        import_zip_bundles()
        course_path = pick_course_interactive()
        if not course_path:
            return 1
        args.list_file = str(course_path)
    elif choice == "4":
        args.from_page = ask("URL del catalogo")
    else:
        ui_print("Opcion no valida.", "err")
        return 1

    if choice == "4":
        args.extract_only = True
        code = run_pipeline(args)
        if code != 0:
            return code
        args.extract_only = False
        course_path = pick_course_interactive()
        if not course_path:
            return 1
        args.list_file = str(course_path)
        args.from_page = None
        return run_pipeline(args)

    return run_pipeline(args)


def main() -> int:
    setup_windows_console()
    ensure_requirements()
    bootstrap_rich_ui()
    init_console()
    bootstrap_app()
    port = start_import_server()
    if port and ui_enabled() and console:
        console.print(
            f"[muted]Importacion automatica desde navegador: "
            f"http://{IMPORT_SERVER_HOST}:{port}[/muted]\n"
        )

    if len(sys.argv) == 1:
        try:
            return interactive_menu()
        except Exception as exc:
            ui_print(f"\n[err]Error:[/err] {exc}", "err")
            if ui_enabled() and console:
                console.print_exception(show_locals=False)
            else:
                import traceback

                traceback.print_exc()
            return 1
        finally:
            stop_import_server()
            pause_if_interactive()

    args = build_parser().parse_args()
    if not args.list_file and not args.from_page and not args.from_json:
        build_parser().print_help()
        return 1

    os.chdir(APP_DIR)
    bootstrap_app()
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
