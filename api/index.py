import sys
import asyncio
import json
import os
import random
import string
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from openai import OpenAI
from pydantic import BaseModel

 
app = FastAPI()
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
 
GROK_API_KEY   = os.environ.get("GROK_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
 
# --- Archivos RAG ------------------------------------------------------------
DIR = os.path.dirname(__file__)
CACHE_FILE    = os.path.join(DIR, "preguntas_cache.json")
PARTIDO_FILE  = os.path.join(DIR, "partido_cache.json")
SALAS_FILE    = os.path.join(DIR, "salas_cache.json")
PARTIDAS_FILE = os.path.join(DIR, "partidas_cache.json")
HISTORIAL_FILE = os.path.join(DIR, "partidos_historial.json")
 
# --- Cliente Groq -------------------------------------------------------------
grok_client = None
if GROK_API_KEY:
    grok_client = OpenAI(
        api_key=GROK_API_KEY,
        base_url="https://api.groq.com/openai/v1"
    )

# --- Cliente Gemini (fallback) ------------------------------------------------
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
 
 
# ===============================================================================
#  MODELOS Pydantic
# ===============================================================================
 
class CrearSalaRequest(BaseModel):
    nombre_jugador: str
 
class UnirseRequest(BaseModel):
    codigo_sala: str
    nombre_jugador: str
 
class GuardarResultadoRequest(BaseModel):
    codigo_sala: str
    nombre_jugador: str
    puntaje: int
    respuestas_correctas: int
    total_preguntas: int
    clave_partido: str = ""
 
 
# ===============================================================================
#  HELPERS RAG - partido y preguntas
# ===============================================================================
 
def cargar_partido_rag() -> dict:
    if os.path.exists(PARTIDO_FILE):
        try:
            with open(PARTIDO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
 
 
def guardar_partido_rag(partido: dict):
    with open(PARTIDO_FILE, "w", encoding="utf-8") as f:
        json.dump(partido, f, ensure_ascii=False, indent=2)
 
 
def cargar_preguntas_rag() -> list:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) >= 10:
                    return data
        except Exception:
            pass
    return []
 
 
def guardar_preguntas_rag(preguntas: list):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(preguntas, f, ensure_ascii=False, indent=2)
 
 
def limpiar_rag():
    for f in [CACHE_FILE, PARTIDO_FILE]:
        if os.path.exists(f):
            os.remove(f)
 
 
# ===============================================================================
#  HELPERS RAG - historial de partidos jugables
# ===============================================================================

def cargar_historial() -> list:
    if os.path.exists(HISTORIAL_FILE):
        try:
            with open(HISTORIAL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def guardar_historial(historial: list):
    with open(HISTORIAL_FILE, "w", encoding="utf-8") as f:
        json.dump(historial, f, ensure_ascii=False, indent=2)


def obtener_partido_historial(clave: str) -> dict:
    for p in cargar_historial():
        if p.get("clave") == clave:
            return p
    return {}


def obtener_ultimo_del_historial() -> dict:
    for p in cargar_historial():
        if p.get("ultimo"):
            return p
    return {}


def upsert_partido_historial(partido_info: dict, preguntas: list = None) -> dict:
    historial = cargar_historial()
    clave = partido_info.get("clave", "")

    existente = None
    for p in historial:
        if p.get("clave") == clave:
            existente = p
        p["ultimo"] = False

    if existente:
        existente.update({
            "descripcion": partido_info.get("descripcion", existente.get("descripcion", "")),
            "tipo":        partido_info.get("tipo", existente.get("tipo", "finalizado")),
            "contexto":    partido_info.get("contexto", existente.get("contexto", "")),
            "fixture_id":  partido_info.get("fixture_id", existente.get("fixture_id")),
            "liga_id":     partido_info.get("liga_id",    existente.get("liga_id")),
            "liga_nombre": partido_info.get("liga_nombre", existente.get("liga_nombre", "")),
            "season":      partido_info.get("season",     existente.get("season")),
            "ultimo":      True,
        })
        if preguntas:
            existente["preguntas"] = preguntas
        item = existente
    else:
        item = {
            "clave":       clave,
            "descripcion": partido_info.get("descripcion", ""),
            "tipo":        partido_info.get("tipo", "finalizado"),
            "contexto":    partido_info.get("contexto", ""),
            "fixture_id":  partido_info.get("fixture_id"),
            "liga_id":     partido_info.get("liga_id"),
            "liga_nombre": partido_info.get("liga_nombre", ""),
            "season":      partido_info.get("season"),
            "preguntas":   preguntas or [],
            "ultimo":      True,
            "guardado_en": datetime.utcnow().isoformat(),
        }
        historial.append(item)

    guardar_historial(historial)
    return item


def guardar_preguntas_partido_historial(clave: str, preguntas: list):
    historial = cargar_historial()
    for p in historial:
        if p.get("clave") == clave:
            p["preguntas"] = preguntas
            guardar_historial(historial)
            return True
    return False

 
 
# ===============================================================================
#  HELPERS RAG - salas y partidas
# ===============================================================================
 
def cargar_salas() -> dict:
    if os.path.exists(SALAS_FILE):
        try:
            with open(SALAS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
 
 
def guardar_salas(salas: dict):
    with open(SALAS_FILE, "w", encoding="utf-8") as f:
        json.dump(salas, f, ensure_ascii=False, indent=2)
 
 
def cargar_partidas() -> list:
    if os.path.exists(PARTIDAS_FILE):
        try:
            with open(PARTIDAS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []
 
 
def guardar_partidas(partidas: list):
    with open(PARTIDAS_FILE, "w", encoding="utf-8") as f:
        json.dump(partidas, f, ensure_ascii=False, indent=2)
 
 
def generar_codigo_sala(longitud: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=longitud))
 
 
# ===============================================================================
#  FOOTBALL API - constantes
# ===============================================================================
 
API_FOOTBALL_BASE    = "https://v3.football.api-sports.io"
MUNDIAL_2026_IDS    = [1, 732]
MUNDIAL_2026_SEASON = 2026



# ===============================================================================
#  FOOTBALL API: buscar partido (en vivo > finalizado HOY > último finalizado)
# ===============================================================================

def obtener_ultimo_partido_api_football(league_id: int = None, season: int = None) -> dict:
    """
    Obtiene el último partido (finalizado o en curso) del Mundial 2026
    (o de la liga indicada por league_id) usando la API-Football (api-sports.io).
    """
    if not FOOTBALL_API_KEY:
        print("[API-FOOTBALL] No hay FOOTBALL_API_KEY configurada.")
        return {}

    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key": FOOTBALL_API_KEY,
    }

    try:
        fixture_data = None
        tipo = None
        
        # 1. RESOLUCIÓN DE ID: Si league_id es None o es una lista, extraemos un entero estricto.
        if league_id is None:
            # Si MUNDIAL_2026_IDS es una lista [1] o similar, tomamos el primer elemento numérico
            if isinstance(MUNDIAL_2026_IDS, list):
                league_id_usado = MUNDIAL_2026_IDS[0]
            else:
                league_id_usado = MUNDIAL_2026_IDS
            seasons_a_probar = [MUNDIAL_2026_SEASON]
        elif isinstance(league_id, list):
            league_id_usado = league_id[0]
            seasons_a_probar = [MUNDIAL_2026_SEASON]
        else:
            league_id_usado = league_id
            seasons_a_probar = [season] if season is not None else [MUNDIAL_2026_SEASON]

        for s in seasons_a_probar:
            # --- PASO 1: INTENTAR BUSCAR EN VIVO ---
            try:
                res_live = requests.get(
                    f"{API_FOOTBALL_BASE}/fixtures",
                    params={"league": int(league_id_usado), "season": int(s), "live": "all"},
                    headers=headers,
                    timeout=5,
                )
                if res_live.status_code == 200:
                    res_json = res_live.json()
                    # Muestra en logs si la API devolvió un mensaje de error de plan o cuota
                    if res_json.get("errors"):
                        print(f"[API-FOOTBALL] Error devuelto por la API: {res_json.get('errors')}")
                    
                    fixtures_live = res_json.get("response", [])
                    if fixtures_live and len(fixtures_live) > 0:
                        fixture_data = fixtures_live[0]
                        tipo = "en_curso"
                        print(f"[API-FOOTBALL] En vivo encontrado ✅ liga={league_id_usado}")
                        break
            except Exception as e:
                print(f"[API-FOOTBALL] Fallo en sub-búsqueda en vivo: {e}")

            # --- PASO 2: SOLUCIÓN DE FALLBACK - BUSCAR HISTORIAL POR ESTADO ---
            # En vez de "last=1" que a veces viene vacío, pedimos los partidos finalizados de la temporada.
            try:
                res_fin = requests.get(
                    f"{API_FOOTBALL_BASE}/fixtures",
                    params={"league": int(league_id_usado), "season": int(s), "status": "FT"},
                    headers=headers,
                    timeout=5,
                )
                if res_fin.status_code == 200:
                    res_json = res_fin.json()
                    fixtures_fin = res_json.get("response", [])
                    
                    if fixtures_fin and len(fixtures_fin) > 0:
                        # Ordenamos los partidos por fecha para asegurar que el último sea el más reciente
                        fixtures_fin.sort(key=lambda x: x.get("fixture", {}).get("date", ""), reverse=True)
                        fixture_data = fixtures_fin[0]  # Tomamos el último partido jugado y finalizado cronológicamente
                        tipo = "finalizado"
                        print(f"[API-FOOTBALL] Partido finalizado recuperado con éxito ✅ liga={league_id_usado}")
                        break
            except Exception as e:
                print(f"[API-FOOTBALL] Fallo en sub-búsqueda de historial: {e}")

        if not fixture_data:
            print(f"[API-FOOTBALL] No se encontraron partidos para la liga={league_id_usado} y temporada={seasons_a_probar}")
            return {}

        # --- Extracción e indexación segura de variables del JSON ---
        fix = fixture_data.get("fixture", {})
        league = fixture_data.get("league", {})
        teams = fixture_data.get("teams", {})
        goals = fixture_data.get("goals", {})
        score = fixture_data.get("score", {})
        events = fixture_data.get("events", [])
        
        fixture_id = fix.get("id")
        fecha = fix.get("date", "")
        estadio = fix.get("venue", {}).get("name", "")
        ciudad = fix.get("venue", {}).get("city", "")
        arbitro = fix.get("referee", "")
        ronda = league.get("round", "")
        home = teams.get("home", {}).get("name", "")
        away = teams.get("away", {}).get("name", "")
        
        goles_h = goals.get("home") if goals.get("home") is not None else "-"
        goles_a = goals.get("away") if goals.get("away") is not None else "-"
        
        pen_h = score.get("penalty", {}).get("home")
        pen_a = score.get("penalty", {}).get("away")
        penales_str = f" (pen. {pen_h}-{pen_a})" if pen_h is not None and pen_a is not None else ""
        
        descripcion = f"{ronda}: {home} {goles_h}{penales_str}-{goles_a} {away}".strip(": ")
        clave = f"{league_id_usado}_{fixture_id}"
        
        # Petición rápida de eventos si el objeto vino simplificado
        if not events:
            try:
                res_detail = requests.get(
                    f"{API_FOOTBALL_BASE}/fixtures",
                    params={"id": int(fixture_id)},
                    headers=headers,
                    timeout=4,
                )
                if res_detail.status_code == 200:
                    det_list = res_detail.json().get("response", [])
                    if det_list and len(det_list) > 0:
                        events = det_list[0].get("events", [])
            except Exception as e:
                print(f"[API-FOOTBALL] Error buscando detalle de eventos: {e}")
        
        eventos_texto = []
        if isinstance(events, list):
            for ev in events[:20]:
                minuto = ev.get("time", {}).get("elapsed", "")
                jugador = ev.get("player", {}).get("name", "")
                detalle = ev.get("detail", "")
                equipo = ev.get("team", {}).get("name", "")
                if jugador and detalle:
                    eventos_texto.append(f"min.{minuto} {detalle} {jugador} ({equipo})")
                    
        eventos_str = "; ".join(eventos_texto) if eventos_texto else ""
        
        contexto = (
            f"{descripcion}. Fecha: {fecha}. "
            f"Estadio: {estadio}{', ' + ciudad if ciudad else ''}. "
            f"Árbitro: {arbitro}."
            f"{(' Eventos: ' + eventos_str + '.') if eventos_str else ''}"
        )
        
        return {
            "fixture_id": fixture_id,
            "clave": clave,
            "descripcion": descripcion,
            "tipo": tipo,
            "contexto": contexto,
            "league_id": league_id_usado,
            "season": league.get("season"),
        }
        
    except Exception as e:
        print(f"[API-FOOTBALL] Excepción inesperada general en procesamiento: {e}")
        return {}

def obtener_jugadores_api_football(fixture_id) -> list:
    if not FOOTBALL_API_KEY:
        return []

    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key":  FOOTBALL_API_KEY,
    }
    jugadores = []
    try:
        res = requests.get(
            f"{API_FOOTBALL_BASE}/fixtures/players",
            params={"fixture": fixture_id},
            headers=headers,
            timeout=8,
        )
        if res.status_code != 200:
            print(f"[API-FOOTBALL] fixtures/players devolvio status {res.status_code}")
            return []

        for team_block in res.json().get("response", []):
            for player_data in team_block.get("players", []):
                info  = player_data.get("player", {})
                stats = (player_data.get("statistics") or [{}])[0]
                games = stats.get("games", {})
                goles = stats.get("goals", {})
                tiros = stats.get("shots", {})
                pases = stats.get("passes", {})
                faltas = stats.get("fouls", {})
                tarj  = stats.get("cards", {})
                gk    = stats.get("goalkeeper", {})

                jugadores.append({
                    "nombre":             info.get("name", ""),
                    "posicion":           games.get("position", "N/A"),
                    "minutos":            games.get("minutes", 0),
                    "calificacion":       games.get("rating", "N/A"),
                    "goles":              goles.get("total", 0) or 0,
                    "asistencias":        goles.get("assists", 0) or 0,
                    "tiros_total":        tiros.get("total", 0) or 0,
                    "tiros_al_arco":      tiros.get("on", 0) or 0,
                    "pases_completados":  pases.get("accuracy", "0"),
                    "faltas_cometidas":   faltas.get("committed", 0) or 0,
                    "faltas_recibidas":   faltas.get("drawn", 0) or 0,
                    "tarjetas_amarillas": tarj.get("yellow", 0) or 0,
                    "tarjetas_rojas":     tarj.get("red", 0) or 0,
                    "atajadas":           gk.get("saves", 0) or 0,
                })
    except Exception as e:
        print(f"[API-FOOTBALL] Error obteniendo jugadores: {e}")
        return []

    return jugadores


# ===============================================================================
#  IA: generar preguntas de trivia
# ===============================================================================
def _llamar_ia(client, model: str, prompt: str, nombre: str) -> str:
    print(f"[IA] Llamando a {nombre} (modelo: {model})...")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=7000,
        timeout=45,
    )
    raw = response.choices[0].message.content
    print(f"[IA] {nombre} respondio OK ({len(raw)} chars)")
    return raw


def _generar_preguntas_ia(partido_info: dict, jugadores: list) -> list:
    if not grok_client and not gemini_client:
        print("[IA] ERROR: no hay ninguna IA configurada")
        return []

    print(f"[IA] Iniciando generacion de preguntas para: {partido_info.get('clave', 'sin clave')}")
    contexto_partido = partido_info.get("contexto", partido_info.get("descripcion", ""))
    tipo = partido_info.get("tipo", "finalizado")

    if jugadores:
        jugadores_compactos = []
        for j in jugadores[:22]:
            jugadores_compactos.append({
                "nombre": j.get("nombre"),
                "goles": j.get("goles"),
                "tiros_al_arco":     j.get("tiros_al_arco", 0),
                "faltas_cometidas":  j.get("faltas_cometidas", 0),
                "atajadas":          j.get("atajadas", 0),
                "tarjetas_amarillas": j.get("tarjetas_amarillas"),
                "tarjetas_rojas": j.get("tarjetas_rojas"),
            })
        contexto_jugadores = (
            f"\n\nEstadisticas reales de jugadores del partido:\n{json.dumps(jugadores_compactos, ensure_ascii=False)}"
        )
    else:
        contexto_jugadores = ""

    estado = "en curso" if tipo == "en_curso" else "ya finalizado"

    prompt = (
        "A continuacion tenes datos OFICIALES extraidos en tiempo real desde la API de futbol "
        f"sobre un partido ({estado}). Estos son los UNICOS datos validos: "
        "no uses tu conocimiento previo sobre otros partidos, no asumas otro resultado, "
        "y no inventes jugadores, goles ni estadisticas que no esten en este texto.\n\n"
        f"DATOS DEL PARTIDO:\n{contexto_partido}"
        f"{contexto_jugadores}\n\n"
        "Basandote ESTRICTAMENTE en los datos anteriores, crea EXACTAMENTE 10 preguntas "
        "de trivia variadas y desafiantes. Incluye preguntas sobre: resultado, goleadores, asistencias, "
        "tarjetas, jugadores destacados, estadisticas, arbitro, estadio, eventos del partido, "
        "contexto historico, records. En ninguna pregunta menciones de donde sacas los datos.\n"
        "Si un dato no aparece en los datos proporcionados, NO generes una pregunta sobre ese dato.\n"
        "IMPORTANTE: todas las respuestas correctas deben ser 100% veridicas segun los datos "
        "proporcionados y corresponder al partido indicado arriba.\n"
        "FORMATO: No uses comillas dobles (\") dentro de los textos de pregunta/opciones/correcta. "
        "Si necesitas citar algo, usa comillas simples (').\n\n"
        "Formato de salida SOLO JSON sin texto adicional ni backticks:\n"
        "{\"preguntas\": [{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}]}"
    )

    raw = None
    intentos = []
    if grok_client:
        intentos.append((grok_client, "llama-3.3-70b-versatile", "Groq"))
    if gemini_client:
        intentos.append((gemini_client, "gemini-2.5-flash", "Gemini"))

    for client, model, nombre in intentos:
        try:
            raw = _llamar_ia(client, model, prompt, nombre)
            break
        except Exception as e:
            print(f"[IA] {nombre} fallo: {e}")

    if not raw:
        return []

    texto = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(texto)
    except json.JSONDecodeError:
        import re
        texto_reparado = re.sub(
            r'(?<=[a-zA-Z0-9aeiouAEIOUnN ,.\\-])"(?=[a-zA-Z0-9aeiouAEIOUnN ,.\\-])',
            r'\\"', texto
        )
        try:
            parsed = json.loads(texto_reparado)
        except json.JSONDecodeError:
            ultimo_corte = texto.rfind("}")
            texto_cortado = texto[:ultimo_corte + 1]
            if not texto_cortado.rstrip().endswith("]}"):
                texto_cortado = texto_cortado.rstrip().rstrip(",") + "]}"
            try:
                parsed = json.loads(texto_cortado)
            except json.JSONDecodeError:
                print("[IA] No se pudo parsear el JSON de la IA tras 3 intentos")
                return []

    return parsed.get("preguntas", [])


def generar_preguntas(partido_info: dict, jugadores: list) -> list:
    return _generar_preguntas_ia(partido_info, jugadores)


def _generar_trivia_generica(liga_id: int, liga_nombre: str) -> list:
    """
    Genera 10 preguntas de trivia general sobre una liga/torneo
    cuando no hay partido disponible hoy.
    Solo se usa para ligas NO mundialistas.
    Para el Mundial, si no hay partido hoy se usa el último partido real.
    """
    if not grok_client and not gemini_client:
        return []

    tema = (
        f"la historia y datos destacados de {liga_nombre}: "
        "campeones historicos, jugadores y goleadores legendarios, "
        "records, partidos memorables, estadios, entrenadores, "
        "datos curiosos, temporadas inolvidables"
    )

    prompt = (
        f"Sos un experto en futbol. Genera EXACTAMENTE 10 preguntas de trivia "
        f"variadas y desafiantes sobre {tema}. "
        "Cubri distintas epocas y aspectos, no repitas el mismo evento. "
        "Cada respuesta correcta debe ser 100% veridica y verificable. "
        "Cada pregunta tiene exactamente 3 opciones (1 correcta, 2 incorrectas plausibles). "
        "No uses comillas dobles (\") dentro de los textos; usa comillas simples (') si hace falta.\n\n"
        "Responde SOLO con este JSON, sin backticks ni texto extra:\n"
        "{\"preguntas\": [{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}]}"
    )

    raw = None
    intentos = []
    if grok_client:
        intentos.append((grok_client, "llama-3.3-70b-versatile", "Groq"))
    if gemini_client:
        intentos.append((gemini_client, "gemini-2.5-flash", "Gemini"))

    for client, model, nombre in intentos:
        try:
            raw = _llamar_ia(client, model, prompt, nombre)
            break
        except Exception as e:
            print(f"[TRIVIA-GEN] {nombre} fallo: {e}")

    if not raw:
        return []

    texto = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(texto)
    except json.JSONDecodeError:
        import re as _re
        rep = _re.sub(
            r'(?<=[a-zA-Z0-9aeiouAEIOUnN ,.\\-])"(?=[a-zA-Z0-9aeiouAEIOUnN ,.\\-])',
            r'\\"', texto
        )
        try:
            parsed = json.loads(rep)
        except json.JSONDecodeError:
            corte = texto.rfind("}")
            txt2  = texto[:corte + 1]
            if not txt2.rstrip().endswith("]}"):
                txt2 = txt2.rstrip().rstrip(",") + "]}"
            try:
                parsed = json.loads(txt2)
            except json.JSONDecodeError:
                print("[TRIVIA-GEN] No se pudo parsear el JSON tras 3 intentos")
                return []

    return parsed.get("preguntas", [])


# ===============================================================================
#  ENDPOINTS
# ===============================================================================
 
@app.get("/", response_class=HTMLResponse)
async def root():
    ruta_html = os.path.join(DIR, "index.html")
    with open(ruta_html, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
 
 
# ===============================================================================
#  ENDPOINT: ligas disponibles
# ===============================================================================

LIGAS_CURADAS = {
    # Internacionales
    2:   ("UEFA Champions League",      "Intl"),
    3:   ("UEFA Europa League",         "Intl"),
    848: ("UEFA Europa Conference Lg",  "Intl"),
    9:   ("Copa America",               "Amer"),
    13:  ("Copa Libertadores",          "Amer"),
    11:  ("Copa Sudamericana",          "Amer"),
    # Europa
    39:  ("Premier League",             "ENG"),
    140: ("La Liga",                    "ESP"),
    135: ("Serie A",                    "ITA"),
    78:  ("Bundesliga",                 "GER"),
    61:  ("Ligue 1",                    "FRA"),
    94:  ("Primeira Liga",              "POR"),
    88:  ("Eredivisie",                 "NED"),
    207: ("Super Lig",                  "TUR"),
    # Sudamerica
    128: ("Liga Argentina",             "ARG"),
    71:  ("Brasileirao",                "BRA"),
    265: ("Liga MX",                    "MEX"),
    239: ("Primera Division Chile",     "CHI"),
    281: ("Liga de Colombia",           "COL"),
    242: ("Liga de Uruguay",            "URU"),
}

@app.get("/api/ligas-disponibles")
async def ligas_disponibles():
    ligas_resultado = []
    mundial_activo = False
    
    if FOOTBALL_API_KEY:
        headers = {
            "x-rapidapi-host": "v3.football.api-sports.io",
            "x-rapidapi-key": FOOTBALL_API_KEY,
        }
        
        # 1. Mundial: Mantener siempre fijo arriba de todo
        mundial_id_activo = None
        for wid in MUNDIAL_2026_IDS:
            try:
                r_live = requests.get(
                    f"{API_FOOTBALL_BASE}/fixtures",
                    params={"league": wid, "season": MUNDIAL_2026_SEASON, "live": "all"},
                    headers=headers, timeout=6,
                )
                if r_live.status_code == 200 and r_live.json().get("response"):
                    mundial_id_activo = wid
                    mundial_activo = True
                    print(f"[LIGAS] [OK] Mundial 2026 EN VIVO con league_id={wid}")
                    break
                
                r_last = requests.get(
                    f"{API_FOOTBALL_BASE}/fixtures",
                    params={"league": wid, "season": MUNDIAL_2026_SEASON, "last": 1},
                    headers=headers, timeout=6,
                )
                if r_last.status_code == 200 and r_last.json().get("response"):
                    mundial_id_activo = wid
                    print(f"[LIGAS] [OK] Mundial 2026 con historial, league_id={wid}")
                    break
            except Exception as e:
                print(f"[LIGAS] Error verificando mundial id={wid}: {e}")
                
        ligas_resultado.append({
            "id": mundial_id_activo or MUNDIAL_2026_IDS[0],
            "nombre": " Mundial 2026",
            "pais": "Internacional",
            "badge": "[EN VIVO]" if mundial_activo else "[MUNDIAL]",
            "es_mundial": True,
            "activo": mundial_activo,
            "separador": False,
        })
        
        # 2. Ligas curadas (Priorizamos año corriente 2026)
        TEMPORADAS = [2026, 2025, 2024]
        for lid, (nombre, emoji) in LIGAS_CURADAS.items():
            agregada = False
            for season in TEMPORADAS:
                try:
                    r = requests.get(
                        f"{API_FOOTBALL_BASE}/fixtures",
                        params={"league": lid, "season": season, "last": 1},
                        headers=headers, timeout=5,
                    )
                    if r.status_code == 200:
                        resp = r.json().get("response", [])
                        if resp:
                            pais_api = resp[0].get("league", {}).get("country", "")
                            ligas_resultado.append({
                                "id": lid,
                                "nombre": nombre,
                                "pais": f"{emoji} {pais_api}" if pais_api else emoji,
                                "badge": "[ACTIVA]",
                                "es_mundial": False,
                                "activo": True,
                                "season": season,
                            })
                            print(f"[LIGAS] [OK] {nombre} activa en season={season}")
                            agregada = True
                            break
                except Exception as e:
                    print(f"[LIGAS] Error verificando {nombre} season={season}: {e}")
            
            # FALLBACK: Si no trae datos del último partido de la API, la insertamos igual para que no diga "Sin información"
            if not agregada:
                ligas_resultado.append({
                    "id": lid,
                    "nombre": nombre,
                    "pais": emoji,
                    "badge": "[DISPONIBLE]",
                    "es_mundial": False,
                    "activo": False,
                    "season": MUNDIAL_2026_SEASON,
                })
    else:
        ligas_resultado.append({
            "id": 1, "nombre": " Mundial 2026", "pais": "Internacional",
            "badge": "[MUNDIAL]", "es_mundial": True, "activo": False,
        })
        
    return {
        "ligas": ligas_resultado,
        "mundial_activo": mundial_activo,
        "total": len(ligas_resultado),
    }


@app.get("/api/debug-ligas")
async def debug_ligas():
    if not FOOTBALL_API_KEY:
        return {"error": "FOOTBALL_API_KEY no configurada"}
    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key":  FOOTBALL_API_KEY,
    }
    resultado = {}
    for wid in MUNDIAL_2026_IDS:
        try:
            r = requests.get(
                f"{API_FOOTBALL_BASE}/fixtures",
                params={"league": wid, "season": MUNDIAL_2026_SEASON, "last": 3},
                headers=headers, timeout=6,
            )
            data = r.json()
            resultado[f"league_{wid}"] = {
                "status_code": r.status_code,
                "total_fixtures": len(data.get("response", [])),
                "sample": data.get("response", [])[:1],
                "errors": data.get("errors"),
            }
        except Exception as e:
            resultado[f"league_{wid}"] = {"error": str(e)}
    return resultado


@app.get("/api/mundial-info")
async def mundial_info(liga_id: int = None, season: int = None):
    """
    Devuelve info del partido actual (el "ultimo" del historial).
    SIEMPRE verifica contra la API si hay un partido más reciente,
    comparando contra el último guardado en el HISTORIAL.
    Si hay uno nuevo, lo agrega al historial marcado como 'ultimo'.
    """
    ultimo_guardado = obtener_ultimo_del_historial()
    if liga_id is not None:
        prefijo = f"{liga_id}_"
        candidatos = [p for p in cargar_historial() if p.get("clave", "").startswith(prefijo)]
        if candidatos:
            candidatos.sort(key=lambda p: p.get("guardado_en", ""), reverse=True)
            ultimo_guardado = candidatos[0]
        else:
            ultimo_guardado = {}

    clave_actual = ultimo_guardado.get("clave", "")

    ultimo_partido_api = obtener_ultimo_partido_api_football(league_id=liga_id, season=season)

    hay_partido_nuevo = bool(
        ultimo_partido_api and ultimo_partido_api.get("clave")
        and ultimo_partido_api.get("clave") != clave_actual
    )

    if hay_partido_nuevo or not ultimo_guardado:
        if ultimo_partido_api:
            nuevo_partido = ultimo_partido_api
        elif ultimo_guardado:
            return {**ultimo_guardado, "desde_cache": True, "partido_nuevo_detectado": False}
        else:
            return {"error": "No se pudo obtener información del partido desde la API."}

        item = upsert_partido_historial(nuevo_partido)
        guardar_partido_rag(nuevo_partido)
        if hay_partido_nuevo and os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        return {**item, "desde_cache": False, "partido_nuevo_detectado": hay_partido_nuevo}

    return {**ultimo_guardado, "desde_cache": True, "partido_nuevo_detectado": False}


@app.get("/api/partidos-historial")
async def partidos_historial():
    historial = cargar_historial()
    ultimos = [p for p in historial if p.get("ultimo")]
    resto   = [p for p in historial if not p.get("ultimo")]
    resto.sort(key=lambda p: p.get("guardado_en", ""), reverse=True)

    partidos = []
    for p in ultimos + resto:
        partidos.append({
            "clave":           p.get("clave", ""),
            "descripcion":     p.get("descripcion", ""),
            "tipo":            p.get("tipo", "finalizado"),
            "ultimo":          bool(p.get("ultimo", False)),
            "tiene_preguntas": len(p.get("preguntas", [])) > 0,
            "guardado_en":     p.get("guardado_en", ""),
            "liga_id":         p.get("liga_id"),
            "liga_nombre":     p.get("liga_nombre", ""),
        })

    return {"partidos": partidos, "total": len(partidos)}


@app.get("/api/trivias")
async def obtener_trivias(clave: str = "", refresh: bool = False, liga_id: int = None, season: int = None):
    if clave and clave != "__reset__":
        partido_item = obtener_partido_historial(clave)
        if not partido_item:
            return {"error": f"Partido con clave '{clave}' no encontrado en el historial"}
    else:
        if liga_id is not None:
            prefijo = f"{liga_id}_"
            candidatos = [p for p in cargar_historial() if p.get("clave", "").startswith(prefijo)]
            if candidatos:
                candidatos.sort(key=lambda p: p.get("guardado_en", ""), reverse=True)
                partido_item = candidatos[0]
            else:
                partido_item = {}
        else:
            partido_item = obtener_ultimo_del_historial()

        if not partido_item:
            print(f"[TRIVIAS] No hay historial, consultando API (liga_id={liga_id})...")
            partido_api = obtener_ultimo_partido_api_football(
                league_id=liga_id,
                season=season,
            )
            if partido_api:
                partido_item = upsert_partido_historial(partido_api)
                guardar_partido_rag(partido_api)
                refresh = True
            else:
                return {"error": "No hay partido disponible. La API no devolvio datos."}

    banco = [] if refresh else partido_item.get("preguntas", [])
    print(f"[TRIVIAS] Partido: {partido_item.get('clave')} | preguntas: {len(banco)} | refresh: {refresh}")

    if not banco:
        if not grok_client and not gemini_client:
            return {"error": "No hay IA configurada (GROK_API_KEY ni GEMINI_API_KEY)"}
        try:
            jugadores = []
            fixture_id = partido_item.get("fixture_id")
            if fixture_id:
                jugadores = obtener_jugadores_api_football(fixture_id)
                print(f"[TRIVIAS] Jugadores obtenidos: {len(jugadores)}")

            banco = await asyncio.to_thread(generar_preguntas, partido_item, jugadores)
            print(f"[TRIVIAS] Preguntas generadas: {len(banco)}")
            if banco:
                guardar_preguntas_partido_historial(partido_item.get("clave", ""), banco)
                if partido_item.get("ultimo"):
                    guardar_preguntas_rag(banco)
            else:
                return {"error": "No se pudieron generar preguntas"}
        except Exception as e:
            return {"error": str(e)}

    muestra = random.sample(banco, min(10, len(banco)))
    return {
        "preguntas": muestra,
        "total_banco": len(banco),
        "clave": partido_item.get("clave", ""),
        "partido": partido_item.get("descripcion", ""),
        "tipo": partido_item.get("tipo", "finalizado"),
        "desde_cache": not refresh,
    }


@app.get("/api/trivias-genericas")
async def trivias_genericas(liga_id: int, liga_nombre: str = "", season: int = None):
    """
    Genera trivia general sobre una liga cuando no hay partido hoy.
    NOTA: Para el Mundial esto no debería llamarse, ya que /api/mundial-info
    ahora retorna el último partido finalizado histórico en lugar de sin_partido_hoy.
    Se mantiene para otras ligas.
    """
    if not liga_nombre:
        liga_nombre = LIGAS_CURADAS.get(liga_id, (f"Liga {liga_id}", ""))[0]

    try:
        preguntas = await asyncio.to_thread(
            _generar_trivia_generica, liga_id, liga_nombre
        )
        if not preguntas:
            return {"error": f"No se pudieron generar preguntas para {liga_nombre}."}

        clave_gen = f"generica_{liga_id}_{datetime.now().strftime('%Y%m%d')}"
        item_historial = {
            "clave":       clave_gen,
            "descripcion": f"Trivia general — {liga_nombre}",
            "tipo":        "generica",
            "contexto":    f"Trivia general sobre {liga_nombre}",
            "fixture_id":  None,
            "liga_id":     liga_id,
            "liga_nombre": liga_nombre,
            "season":      season,
        }
        upsert_partido_historial(item_historial, preguntas)

        return {
            "preguntas":   preguntas,
            "total_banco": len(preguntas),
            "clave":       clave_gen,
            "partido":     f"Trivia general — {liga_nombre}",
            "tipo":        "generica",
            "desde_cache": False,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/test")
async def probar_apis():
    partido = cargar_partido_rag()
    preguntas = cargar_preguntas_rag()
    return {
        "partido_rag": partido,
        "preguntas_en_cache": len(preguntas),
        "grok_configurado": grok_client is not None,
        "football_api_configurada": bool(FOOTBALL_API_KEY),
    }
 
 
# ===============================================================================
#  ENDPOINTS - salas y resultados
# ===============================================================================
 
@app.post("/api/salas/crear")
async def crear_sala(body: CrearSalaRequest):
    salas = cargar_salas()
    codigo = generar_codigo_sala()
    while codigo in salas:
        codigo = generar_codigo_sala()
    partido_rag = cargar_partido_rag()
    salas[codigo] = {
        "codigo": codigo,
        "creada_en": datetime.utcnow().isoformat(),
        "partido": partido_rag.get("descripcion", ""),
        "clave_partido": partido_rag.get("clave", ""),
        "estado": "esperando",
        "jugadores": [{
            "nombre": body.nombre_jugador,
            "unido_en": datetime.utcnow().isoformat(),
            "puntaje": None,
            "finalizo": False,
        }],
    }
    guardar_salas(salas)
    return {
        "codigo_sala": codigo,
        "mensaje": f"Sala {codigo} creada.",
        "sala": salas[codigo],
    }
 
 
@app.post("/api/salas/unirse")
async def unirse_sala(body: UnirseRequest):
    salas = cargar_salas()
    codigo = body.codigo_sala.upper().strip()
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
    sala = salas[codigo]
    nombres_existentes = [j["nombre"].lower() for j in sala["jugadores"]]
    if body.nombre_jugador.lower() in nombres_existentes:
        raise HTTPException(status_code=400, detail=f"El nombre '{body.nombre_jugador}' ya está en uso.")
    sala["jugadores"].append({
        "nombre": body.nombre_jugador,
        "unido_en": datetime.utcnow().isoformat(),
        "puntaje": None,
        "finalizo": False,
    })
    guardar_salas(salas)
    return {"mensaje": f"{body.nombre_jugador} se unio a la sala {codigo}!", "sala": sala}
 
 
@app.post("/api/salas/resultado")
async def guardar_resultado(body: GuardarResultadoRequest):
    salas = cargar_salas()
    codigo = body.codigo_sala.upper().strip()
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
    sala = salas[codigo]
    jugador_encontrado = False
    for jugador in sala["jugadores"]:
        if jugador["nombre"].lower() == body.nombre_jugador.lower():
            jugador["puntaje"] = body.puntaje
            jugador["respuestas_correctas"] = body.respuestas_correctas
            jugador["total_preguntas"] = body.total_preguntas
            jugador["finalizo"] = True
            jugador["finalizado_en"] = datetime.utcnow().isoformat()
            jugador["clave_partido"] = body.clave_partido
            jugador_encontrado = True
            break
    if not jugador_encontrado:
        sala["jugadores"].append({
            "nombre": body.nombre_jugador,
            "unido_en": datetime.utcnow().isoformat(),
            "puntaje": body.puntaje,
            "respuestas_correctas": body.respuestas_correctas,
            "total_preguntas": body.total_preguntas,
            "finalizo": True,
            "finalizado_en": datetime.utcnow().isoformat(),
            "clave_partido": body.clave_partido,
        })
    todos_terminaron = all(j.get("finalizo") for j in sala["jugadores"])
    guardar_salas(salas)
    partidas = cargar_partidas()
    partidas.append({
        "nombre_jugador":       body.nombre_jugador,
        "puntaje":              body.puntaje,
        "respuestas_correctas": body.respuestas_correctas,
        "total_preguntas":      body.total_preguntas,
        "codigo_sala":          codigo,
        "clave_partido":        body.clave_partido,
        "partido":              sala.get("partido", ""),
        "fecha":                datetime.utcnow().isoformat(),
    })
    guardar_partidas(partidas)
    return {"mensaje": "Resultado guardado.", "sala": sala, "todos_terminaron": todos_terminaron}
 
 
@app.get("/api/salas/{codigo}")
async def obtener_sala(codigo: str):
    salas = cargar_salas()
    codigo = codigo.upper().strip()
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
    sala = salas[codigo]
    leaderboard = sorted(
        [j for j in sala["jugadores"] if j["finalizo"]],
        key=lambda x: x["puntaje"], reverse=True,
    )
    return {
        "sala": sala,
        "leaderboard": leaderboard,
        "jugadores_totales": len(sala["jugadores"]),
        "jugadores_terminaron": sum(1 for j in sala["jugadores"] if j["finalizo"]),
    }
 
 
@app.get("/api/salas/{codigo}/ranking-total")
async def ranking_total_sala(codigo: str):
    salas = cargar_salas()
    codigo = codigo.upper().strip()
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
    partidas = cargar_partidas()
    participaciones_sala = [p for p in partidas if p.get("codigo_sala", "").upper() == codigo]
    acumulado: dict[str, dict] = {}
    for p in participaciones_sala:
        nombre = p["nombre_jugador"]
        if nombre not in acumulado:
            acumulado[nombre] = {"nombre": nombre, "puntaje_total": 0, "partidas_jugadas": 0}
        acumulado[nombre]["puntaje_total"] += p.get("puntaje", 0)
        acumulado[nombre]["partidas_jugadas"] += 1
    ranking = sorted(acumulado.values(), key=lambda x: x["puntaje_total"], reverse=True)
    return {
        "codigo_sala": codigo,
        "ranking_total": ranking,
        "total_participaciones": len(participaciones_sala),
        "jugadores_unicos": len(acumulado),
    }


@app.get("/api/leaderboard")
async def leaderboard_global(limite: int = 20):
    partidas = cargar_partidas()
    mejores: dict[str, dict] = {}
    for p in partidas:
        nombre = p["nombre_jugador"]
        if nombre not in mejores or p["puntaje"] > mejores[nombre]["puntaje"]:
            mejores[nombre] = p
    ranking = sorted(mejores.values(), key=lambda x: x["puntaje"], reverse=True)[:limite]
    return {
        "leaderboard": ranking,
        "total_partidas_jugadas": len(partidas),
        "total_jugadores_unicos": len(mejores),
    }
 
 
@app.get("/api/jugador/{nombre}/historial")
async def historial_jugador(nombre: str):
    partidas = cargar_partidas()
    historial = [p for p in partidas if p["nombre_jugador"].lower() == nombre.lower()]
    historial.sort(key=lambda x: x["fecha"], reverse=True)
    if not historial:
        return {"mensaje": f"No se encontraron partidas para '{nombre}'.", "historial": []}
    mejor = max(historial, key=lambda x: x["puntaje"])
    return {
        "nombre_jugador": nombre,
        "partidas_jugadas": len(historial),
        "mejor_puntaje": mejor["puntaje"],
        "historial": historial,
    }
@app.get("/api/test-football-key")
async def test_football_key():
    """
    Endpoint de diagnóstico para validar el estado de la API-Football
    sin consumir la cuota de peticiones diarias.
    """
    if not FOOTBALL_API_KEY:
        return {
            "estado": "ERROR",
            "motivo": "La variable FOOTBALL_API_KEY no está configurada en las variables de entorno."
        }
        
    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key": FOOTBALL_API_KEY
    }
    
    diagnostico = {}
    
    try:
        # Probamos el endpoint /status (No consume créditos diarios)
        url_status = f"{API_FOOTBALL_BASE}/status"
        response = requests.get(url_status, headers=headers, timeout=5)
        
        diagnostico["http_status_code"] = response.status_code
        
        if response.status_code == 200:
            data = response.json()
            
            # Verificamos errores internos de la API (ej. API Key inválida)
            if data.get("errors"):
                diagnostico["estado"] = "FALLIDO"
                diagnostico["detalles_error"] = data.get("errors")
                diagnostico["sugerencia"] = "Revisa si copiaste bien la clave en tu entorno de desarrollo."
            else:
                resp_info = data.get("response", {})
                account = resp_info.get("account", {})
                sub = resp_info.get("subscription", {})
                reqs = resp_info.get("requests", {})
                
                diagnostico["estado"] = "CONEXIÓN EXITOSA"
                diagnostico["usuario"] = f"{account.get('firstname')} {account.get('lastname')}"
                diagnostico["plan"] = sub.get("plan")
                diagnostico["peticiones_hoy"] = f"{reqs.get('current')} / {reqs.get('limit_day')}"
                
                # Alerta si superaste el límite del plan
                if reqs.get("current", 0) >= reqs.get("limit_day", 100):
                    diagnostico["alerta"] = "Has alcanzado el límite diario de llamadas de tu plan."
        
        elif response.status_code == 401:
            diagnostico["estado"] = "NO AUTORIZADO (401)"
            diagnostico["sugerencia"] = "Tu API Key no es válida o fue revocada en el panel de control de API-Sports."
        elif response.status_code == 429:
            diagnostico["estado"] = "LÍMITE SUPERADO (429)"
            diagnostico["sugerencia"] = "Estás haciendo demasiadas peticiones por minuto para tu plan (el plan gratuito permite 10 por minuto)."
        else:
            diagnostico["estado"] = f"ERROR HTTP {response.status_code}"
            diagnostico["respuesta_servidor"] = response.text

    except requests.exceptions.Timeout:
        diagnostico["estado"] = "TIMEOUT"
        diagnostico["motivo"] = "El servidor de API-Football tardó demasiado en responder. Revisa tu conexión a internet."
    except Exception as e:
        diagnostico["estado"] = "EXCEPCIÓN INESPERADA"
        diagnostico["error"] = str(e)
        
    return diagnostico
@app.get("/api/test-mundial")
async def test_mundial():
    """
    Endpoint de testeo para ver la respuesta exacta de la API
    para el Mundial 2026 y descubrir por qué falla la extracción.
    """
    if not FOOTBALL_API_KEY:
        return {"error": "FOOTBALL_API_KEY no configurada"}
        
    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key": FOOTBALL_API_KEY,
    }
    
    reporte_diagnostico = {}
    
    # Probamos los dos IDs típicos del mundial configurados en tu código
    ids_a_probar = [1, 732]
    
    for wid in ids_a_probar:
        reporte_diagnostico[f"league_id_{wid}"] = {}
        try:
            # 1. Testear consulta de último partido
            url = f"{API_FOOTBALL_BASE}/fixtures"
            params = {"league": wid, "season": 2026, "last": 1}
            
            res = requests.get(url, params=params, headers=headers, timeout=6)
            
            if res.status_code == 200:
                json_data = res.json()
                lista_partidos = json_data.get("response", [])
                
                reporte_diagnostico[f"league_id_{wid}"]["status_http"] = 200
                reporte_diagnostico[f"league_id_{wid}"]["errores_api"] = json_data.get("errors")
                reporte_diagnostico[f"league_id_{wid}"]["cantidad_partidos_devueltos"] = len(lista_partidos)
                
                if lista_partidos:
                    # Si devuelve algo, guardamos una muestra simplificada para ver la estructura exacta
                    partido_ejemplo = lista_partidos[0]
                    reporte_diagnostico[f"league_id_{wid}"]["estructura_correcta"] = {
                        "fixture_id": partido_ejemplo.get("fixture", {}).get("id"),
                        "teams": partido_ejemplo.get("teams"),
                        "goals": partido_ejemplo.get("goals")
                    }
                else:
                    reporte_diagnostico[f"league_id_{wid}"]["causa_vacio"] = "La API respondió OK, pero la lista de partidos vino vacía. Posiblemente la temporada 2026 de este ID de liga no tiene partidos finalizados aún en su base de datos."
            else:
                reporte_diagnostico[f"league_id_{wid}"]["error_http"] = f"Código de error del servidor: {res.status_code}"
                
        except Exception as e:
            reporte_diagnostico[f"league_id_{wid}"]["excepcion"] = str(e)
            
    return reporte_diagnostico
