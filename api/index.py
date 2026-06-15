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
SALAS_FILE    = os.path.join(DIR, "salas_cache.json")       # ← NUEVO
PARTIDAS_FILE = os.path.join(DIR, "partidas_cache.json")    # ← NUEVO
HISTORIAL_FILE = os.path.join(DIR, "partidos_historial.json")  # ← NUEVO: historial de partidos jugables
 
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
#  HELPERS RAG — partido y preguntas (sin cambios)
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
#  HELPERS RAG — historial de partidos jugables  ← NUEVO
# ===============================================================================

def cargar_historial() -> list:
    """
    Devuelve la lista de partidos guardados, cada uno con su propio banco
    de preguntas. Estructura de cada item:
    {
        "clave": "...", "descripcion": "...", "tipo": "...",
        "contexto": "...", "fixture_id": ..., "preguntas": [...],
        "ultimo": bool, "guardado_en": iso-datetime
    }
    """
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
    """Busca un partido por clave dentro del historial."""
    for p in cargar_historial():
        if p.get("clave") == clave:
            return p
    return {}


def obtener_ultimo_del_historial() -> dict:
    """Devuelve el partido marcado como 'ultimo' en el historial (o {} si no hay)."""
    for p in cargar_historial():
        if p.get("ultimo"):
            return p
    return {}


def upsert_partido_historial(partido_info: dict, preguntas: list = None) -> dict:
    """
    Inserta o actualiza un partido en el historial. Marca este partido
    como 'ultimo' y desmarca a todos los demás. Si ya existía un item
    con la misma clave, conserva sus preguntas salvo que se pasen nuevas.
    Devuelve el item guardado.
    """
    historial = cargar_historial()
    clave = partido_info.get("clave", "")

    existente = None
    for p in historial:
        if p.get("clave") == clave:
            existente = p
        p["ultimo"] = False  # desmarcar todos

    if existente:
        existente.update({
            "descripcion": partido_info.get("descripcion", existente.get("descripcion", "")),
            "tipo":        partido_info.get("tipo", existente.get("tipo", "finalizado")),
            "contexto":    partido_info.get("contexto", existente.get("contexto", "")),
            "fixture_id":  partido_info.get("fixture_id", existente.get("fixture_id")),
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
            "preguntas":   preguntas or [],
            "ultimo":      True,
            "guardado_en": datetime.utcnow().isoformat(),
        }
        historial.append(item)

    guardar_historial(historial)
    return item


def guardar_preguntas_partido_historial(clave: str, preguntas: list):
    """Guarda/actualiza el banco de preguntas de un partido específico del historial."""
    historial = cargar_historial()
    for p in historial:
        if p.get("clave") == clave:
            p["preguntas"] = preguntas
            guardar_historial(historial)
            return True
    return False

 
 
# ===============================================================================
#  HELPERS RAG — salas y partidas  ← NUEVO
# ===============================================================================
 
def cargar_salas() -> dict:
    """Devuelve el dict de salas { codigo: { ...datos de la sala } }."""
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
    """Devuelve la lista global de partidas jugadas."""
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
    """Genera un código alfanumérico único para la sala."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=longitud))
 
 
# ===============================================================================
#  FOOTBALL API
# ===============================================================================
 
def obtener_jugadores_fixture(fixture_id) -> list:
    """
    Obtiene estadisticas de jugadores del partido desde el endpoint
    'summary' de ESPN, mapeando a las mismas claves que antes.
    """
    jugadores = []
    try:
        res = requests.get(
            ESPN_SUMMARY_URL,
            params={"event": fixture_id},
            timeout=6
        )
        if res.status_code != 200:
            return jugadores

        data = res.json()
        rosters = data.get("rosters", [])

        # Mapa de claves de stats de ESPN -> nombres de stat (varia por deporte/version)
        for team in rosters:
            for player in team.get("roster", []):
                atleta = player.get("athlete", {})
                nombre = atleta.get("displayName", "")
                posicion = atleta.get("position", {}).get("abbreviation", "N/A")

                stats_dict = {}
                for stat_grupo in player.get("stats", []):
                    nombre_stat = stat_grupo.get("name") or stat_grupo.get("abbreviation")
                    valor_stat = stat_grupo.get("value", stat_grupo.get("displayValue"))
                    if nombre_stat is not None:
                        stats_dict[nombre_stat] = valor_stat

                jugadores.append({
                    "nombre":            nombre,
                    "posicion":          posicion,
                    "minutos":           stats_dict.get("minutes", stats_dict.get("appearances", 0)),
                    "calificacion":      stats_dict.get("rating", "N/A"),
                    "goles":             stats_dict.get("goals", 0),
                    "asistencias":       stats_dict.get("goalAssists", stats_dict.get("assists", 0)),
                    "tiros_total":       stats_dict.get("totalShots", 0),
                    "tiros_al_arco":     stats_dict.get("shotsOnTarget", 0),
                    "pases_completados": stats_dict.get("accuratePasses", "0"),
                    "faltas_cometidas":  stats_dict.get("foulsCommitted", 0),
                    "faltas_recibidas":  stats_dict.get("foulsSuffered", stats_dict.get("foulsDrawn", 0)),
                    "tarjetas_amarillas":stats_dict.get("yellowCards", 0),
                    "tarjetas_rojas":    stats_dict.get("redCards", 0),
                    "atajadas":          stats_dict.get("saves", 0),
                })
    except Exception:
        pass
    return jugadores
 
 
# ===============================================================================
#  FOOTBALL API: último partido jugado del Mundial 2026
# ===============================================================================

MUNDIAL_2026_LEAGUE_ID = 1   # (legado, ya no se usa con ESPN)
MUNDIAL_2026_SEASON    = 2026

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_SUMMARY_URL    = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary"


import requests
from datetime import datetime


# ===============================================================================
#  FOOTBALL API: último partido jugado del Mundial 2026 — API-Football oficial
# ===============================================================================

API_FOOTBALL_BASE    = "https://v3.football.api-sports.io"
# IDs posibles del Mundial 2026 en api-football:
# 1 = FIFA World Cup (histórico), 732 = FIFA World Cup 2026 (puede variar según proveedor)
# Se prueban ambos automáticamente.
MUNDIAL_2026_IDS    = [1, 732]
MUNDIAL_2026_SEASON = 2026


def _buscar_fixture_mundial(headers: dict) -> tuple:
    """
    Intenta obtener fixtures del Mundial 2026 probando los IDs 1 y 732.
    Devuelve (fixture_data, tipo, league_id_usado) o (None, None, None) si no hay datos.
    Primero busca partidos en vivo, luego finalizados.
    """
    for league_id in MUNDIAL_2026_IDS:
        print(f"[API-FOOTBALL] Probando Mundial con league_id={league_id}...")

        # Buscar en vivo
        try:
            res_live = requests.get(
                f"{API_FOOTBALL_BASE}/fixtures",
                params={"league": league_id, "season": MUNDIAL_2026_SEASON, "live": "all"},
                headers=headers,
                timeout=8,
            )
            if res_live.status_code == 200:
                fixtures_live = res_live.json().get("response", [])
                if fixtures_live:
                    print(f"[API-FOOTBALL] ✅ Partido en vivo encontrado con league_id={league_id}")
                    return fixtures_live[0], "en_curso", league_id
        except Exception as e:
            print(f"[API-FOOTBALL] Error buscando en vivo (league={league_id}): {e}")

        # Buscar finalizado
        try:
            res_fin = requests.get(
                f"{API_FOOTBALL_BASE}/fixtures",
                params={
                    "league": league_id,
                    "season": MUNDIAL_2026_SEASON,
                    "status": "FT-AET-PEN",
                    "last":   1,
                },
                headers=headers,
                timeout=8,
            )
            if res_fin.status_code == 200:
                fixtures_fin = res_fin.json().get("response", [])
                if fixtures_fin:
                    print(f"[API-FOOTBALL] ✅ Partido finalizado encontrado con league_id={league_id}")
                    return fixtures_fin[0], "finalizado", league_id
            else:
                print(f"[API-FOOTBALL] HTTP {res_fin.status_code} con league_id={league_id}")
        except Exception as e:
            print(f"[API-FOOTBALL] Error buscando finalizado (league={league_id}): {e}")

    print("[API-FOOTBALL] ⚠️ No se encontraron fixtures del Mundial con ninguno de los IDs probados.")
    return None, None, None


def obtener_ultimo_partido_api_football(league_id: int = None, season: int = None) -> dict:
    """
    Obtiene el último partido (finalizado o en curso) del Mundial 2026
    (o de la liga indicada por league_id) usando la API-Football (api-sports.io).
    Si se pasa season, usa esa temporada. Si no, para ligas no-Mundial prueba
    2025 y 2026 automáticamente (ligas europeas usan season=2025).
    """
    if not FOOTBALL_API_KEY:
        print("[API-FOOTBALL] No hay FOOTBALL_API_KEY configurada, usando ESPN como fallback.")
        return obtener_ultimo_partido_mundial2026_ESPN_DESUSO()

    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key": FOOTBALL_API_KEY,
    }

    try:
        fixture_data = None
        tipo = None
        league_id_usado = league_id  # Para liga específica; se sobreescribe en bloque Mundial

        if league_id is not None:
            # ── Liga específica solicitada ──────────────────────────────────────
            # Si se pasó season explícita usarla, sino probar 2025 y 2026
            seasons_a_probar = [season] if season else [2025, 2026, 2024]

            for s in seasons_a_probar:
                if fixture_data:
                    break
                # Buscar en vivo primero
                try:
                    res_live = requests.get(
                        f"{API_FOOTBALL_BASE}/fixtures",
                        params={"league": league_id, "season": s, "live": "all"},
                        headers=headers,
                        timeout=8,
                    )
                    if res_live.status_code == 200:
                        fixtures_live = res_live.json().get("response", [])
                        if fixtures_live:
                            fixture_data = fixtures_live[0]
                            tipo = "en_curso"
                            print(f"[API-FOOTBALL] ✅ En vivo encontrado liga={league_id} season={s}")
                            break
                except Exception as e:
                    print(f"[API-FOOTBALL] Error en vivo liga={league_id} season={s}: {e}")

                # Luego buscar último finalizado
                try:
                    res_fin = requests.get(
                        f"{API_FOOTBALL_BASE}/fixtures",
                        params={
                            "league": league_id,
                            "season": s,
                            "status": "FT-AET-PEN",
                            "last":   1,
                        },
                        headers=headers,
                        timeout=8,
                    )
                    if res_fin.status_code == 200:
                        fixtures_fin = res_fin.json().get("response", [])
                        if fixtures_fin:
                            fixture_data = fixtures_fin[0]
                            tipo = "finalizado"
                            print(f"[API-FOOTBALL] ✅ Finalizado encontrado liga={league_id} season={s}")
                except Exception as e:
                    print(f"[API-FOOTBALL] Error finalizado liga={league_id} season={s}: {e}")

            if not fixture_data:
                print(f"[API-FOOTBALL] Sin fixtures para league_id={league_id}.")
                return {}
        else:
            # ── Mundial: probar IDs 1 y 732 ─────────────────────────────────────
            fixture_data, tipo, league_id_usado = _buscar_fixture_mundial(headers)
            if not fixture_data:
                return obtener_ultimo_partido_mundial2026_ESPN_DESUSO()

        # ── Extraer datos del fixture ────────────────────────────────────────────
        fix      = fixture_data.get("fixture", {})
        league   = fixture_data.get("league", {})
        teams    = fixture_data.get("teams", {})
        goals    = fixture_data.get("goals", {})
        score    = fixture_data.get("score", {})
        events   = fixture_data.get("events", [])

        fixture_id = fix.get("id")
        fecha      = fix.get("date", "")
        estadio    = fix.get("venue", {}).get("name", "")
        ciudad     = fix.get("venue", {}).get("city", "")
        arbitro    = fix.get("referee", "")
        ronda      = league.get("round", "")

        home       = teams.get("home", {}).get("name", "")
        away       = teams.get("away", {}).get("name", "")
        goles_h    = goals.get("home", "")
        goles_a    = goals.get("away", "")

        # Penales
        pen_h = score.get("penalty", {}).get("home")
        pen_a = score.get("penalty", {}).get("away")
        penales_str = f" (pen. {pen_h}-{pen_a})" if pen_h is not None and pen_a is not None else ""

        descripcion = f"{ronda}: {home} {goles_h}{penales_str}-{goles_a} {away}".strip(": ")
        # FIX: La clave incluye el league_id para evitar mezcla de preguntas entre ligas
        clave       = f"{league_id_usado}_{fixture_id}"

        # Obtener eventos del partido para el contexto (goles, tarjetas)
        if not events:
            res_detail = requests.get(
                f"{API_FOOTBALL_BASE}/fixtures",
                params={"id": fixture_id},
                headers=headers,
                timeout=8,
            )
            if res_detail.status_code == 200:
                det_list = res_detail.json().get("response", [])
                if det_list:
                    events = det_list[0].get("events", [])

        eventos_texto = []
        for ev in events[:20]:
            minuto  = ev.get("time", {}).get("elapsed", "")
            jugador = ev.get("player", {}).get("name", "")
            detalle = ev.get("detail", "")
            equipo  = ev.get("team", {}).get("name", "")
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
            "fixture_id":  fixture_id,
            "clave":       clave,
            "descripcion": descripcion,
            "tipo":        tipo,
            "contexto":    contexto,
            "league_id":   league_id_usado,
            "season":      fixture_data.get("league", {}).get("season", MUNDIAL_2026_SEASON),
        }

    except Exception as e:
        print(f"[API-FOOTBALL] Excepción: {e}. Fallback a ESPN.")
        return obtener_ultimo_partido_mundial2026_ESPN_DESUSO()


def obtener_jugadores_api_football(fixture_id) -> list:
    """
    Obtiene estadísticas de jugadores del partido desde API-Football.
    Devuelve lista compatible con el formato que espera _generar_preguntas_ia().
    """
    if not FOOTBALL_API_KEY:
        return obtener_jugadores_fixture(fixture_id)

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
            return obtener_jugadores_fixture(fixture_id)

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
        return obtener_jugadores_fixture(fixture_id)

    return jugadores


def obtener_ultimo_partido_mundial2026_ESPN_DESUSO() -> dict:
    """
    [DESUSO] Obtenía el último partido del Mundial 2026 desde ESPN.
    Reemplazada por obtener_ultimo_partido_api_football() que usa la API-Football oficial.
    Se conserva por referencia histórica.
    """
    try:
        # 1. Definir rango de fechas dinámico
        fecha_inicio = "20260611"
        fecha_hoy = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
        url = f"{ESPN_SCOREBOARD_URL}?dates={fecha_inicio}-{fecha_hoy}&limit=100"

        res = requests.get(url, timeout=6)
        if res.status_code != 200:
            return {}

        data = res.json()
        eventos = data.get("events", [])
        if not eventos:
            return {}

        finalizados = []
        en_vivo = []

        for ev in eventos:
            status = ev.get("status", {}).get("type", {})

            # Partido en juego
            if status.get("state") == "in":
                en_vivo.append(ev)

            # Partido finalizado
            if (
                status.get("completed") is True
                or status.get("state") == "post"
                or status.get("name") == "STATUS_FINAL"
            ):
                finalizados.append(ev)

        def mapear_fecha(e):
            try:
                return datetime.fromisoformat(
                    e.get("date", "").replace("Z", "+00:00")
                )
            except ValueError:
                return datetime.min

        # Prioridad: partido en vivo
        if en_vivo:
            en_vivo.sort(key=mapear_fecha, reverse=True)
            evento = en_vivo[0]

        # Si no hay en vivo, usar el último finalizado
        elif finalizados:
            finalizados.sort(key=mapear_fecha, reverse=True)
            evento = finalizados[0]

        # Si no hay ninguno
        else:
            return {}

        fixture_id = evento.get("id")
        fecha = evento.get("date", "")

        competition = (evento.get("competitions") or [{}])[0]
        competidores = competition.get("competitors", [])

        home_data = next((c for c in competidores if c.get("homeAway") == "home"), {})
        away_data = next((c for c in competidores if c.get("homeAway") == "away"), {})

        home = home_data.get("team", {}).get("displayName", "")
        away = away_data.get("team", {}).get("displayName", "")
        goles_home = home_data.get("score", "")
        goles_away = away_data.get("score", "")

        venue = competition.get("venue", {})
        estadio = venue.get("fullName", "")
        ciudad = venue.get("address", {}).get("city", "")

        arbitros = competition.get("officials", [])
        arbitro = arbitros[0].get("displayName", "") if arbitros else ""

        ronda = ""
        if competition.get("notes"):
            ronda = competition["notes"][0].get("headline", "")
        elif evento.get("name"):
            ronda = evento.get("name", "")

        if len(ronda) > 80:
            ronda = ronda[:80].rsplit(" ", 1)[0] + "..."

        descripcion = f"{ronda}: {home} {goles_home}-{goles_away} {away}".strip(": ")

        eventos_texto = []
        for det in competition.get("details", [])[:15]:
            tipo_evento = det.get("type", {}).get("text", "")
            equipo_id = det.get("team", {}).get("id")
            equipo_nombre = home if equipo_id == home_data.get("team", {}).get("id") else away

            if tipo_evento:
                eventos_texto.append(f"{tipo_evento} ({equipo_nombre})")

        eventos_str = ", ".join(eventos_texto) if eventos_texto else ""

        contexto = (
            f"{descripcion}. Fecha: {fecha}. Estadio: {estadio}"
            f"{', ' + ciudad if ciudad else ''}. "
            f"Árbitro: {arbitro}."
            f"{(' Eventos: ' + eventos_str + '.') if eventos_str else ''}"
        )

        return {
            "fixture_id": fixture_id,
            "clave": f"Mundial2026_{fixture_id}",
            "descripcion": descripcion,
            "tipo": "en_curso" if en_vivo else "finalizado",
            "contexto": contexto,
        }

    except Exception as e:
        print(e)
        return {}

# ===============================================================================
#  IA: detectar partido del mundial
# ===============================================================================
 
def detectar_partido_mundial_con_ia() -> dict:
    if not grok_client:
        return {
            "clave": "Qatar2022_Final",
            "descripcion": "Final Qatar 2022: Argentina 3-3 (4-2) Francia",
            "tipo": "finalizado",
            "contexto": "Final del Mundial Qatar 2022 entre Argentina y Francia. Argentina ganó por penales 4-2 tras empatar 3-3. Messi convirtió 2 goles, Mbappé hizo un hat-trick. Emiliano Martínez fue figura en los penales."
        }
 
    prompt = (
        "Eres un experto en fútbol mundial. Necesito saber cuál fue el último partido del mundo 2026 que se jugo "
        "hoy mismo.\n\n"
        "REGLAS:\n"
        "Responde ÚNICAMENTE con este JSON (sin backticks, sin texto extra):\n"
        "{\n"
        '  "clave": "string corto único, ej: Qatar2022_Final o USA2026_Final",\n'
        '  "descripcion": "Texto legible del partido, ej: Final Qatar 2022: Argentina 3-3 (4-2) Francia",\n'
        '  "tipo": "en_curso" o "finalizado",\n'
        '  "contexto": "Resumen detallado del partido con goles, minutos, penales, jugadores destacados, '
        'estadísticas clave, árbitro, estadio, fecha. Todo lo que sea útil para generar trivia."\n'
        "}"
    )
 
    try:
        response = grok_client.chat.completions.create(
            model = "llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800
        )
        raw = response.choices[0].message.content
        texto = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(texto)
    except Exception as e:
        return {
            "clave": "Qatar2022_Final",
            "descripcion": "Final Qatar 2022: Argentina 3-3 (4-2) Francia",
            "tipo": "finalizado",
            "contexto": f"Final del Mundial Qatar 2022. Error al detectar: {str(e)}"
        }
 
 
# ===============================================================================
#  IA: generar 50 preguntas de trivia
# ===============================================================================
def _llamar_ia(client, model: str, prompt: str, nombre: str) -> str:
    """Llama a una IA y devuelve el texto crudo, o lanza excepción si falla."""
    print(f"[IA] Llamando a {nombre} (modelo: {model})...")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=7000,
        timeout=45,
    )
    raw = response.choices[0].message.content
    print(f"[IA] {nombre} respondió OK ({len(raw)} chars)")
    return raw


def _generar_preguntas_ia(partido_info: dict, jugadores: list) -> list:
    if not grok_client and not gemini_client:
        print("[IA] ERROR: no hay ninguna IA configurada (GROK_API_KEY ni GEMINI_API_KEY)")
        return []

    print(f"[IA] Iniciando generación de preguntas para: {partido_info.get('clave', 'sin clave')}")
    contexto_partido = partido_info.get("contexto", partido_info.get("descripcion", ""))
    tipo = partido_info.get("tipo", "finalizado")
    print(f"[IA] Contexto del partido ({len(contexto_partido)} chars): {contexto_partido[:200]}...")

    if jugadores:
        print(f"[IA] Jugadores recibidos: {len(jugadores)}")
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
            f"\n\nEstadísticas reales de jugadores del partido:\n{json.dumps(jugadores_compactos, ensure_ascii=False)}"
        )
    else:
        print("[IA] Sin jugadores — se generarán preguntas solo con contexto del partido")
        contexto_jugadores = ""

    estado = "en curso" if tipo == "en_curso" else "ya finalizado"

    prompt = (
        "A continuación tenés datos OFICIALES extraídos en tiempo real desde la API de fútbol "
        f"sobre un partido ({estado}). Estos son los ÚNICOS datos válidos: "
        "no uses tu conocimiento previo sobre otros partidos, no asumas otro resultado, "
        "y no inventes jugadores, goles ni estadísticas que no estén en este texto.\n\n"
        f"DATOS DEL PARTIDO:\n{contexto_partido}"
        f"{contexto_jugadores}\n\n"
        "Basándote ESTRICTAMENTE en los datos anteriores, crea EXACTAMENTE 10 preguntas "
        "de trivia variadas y desafiantes. Incluye preguntas sobre: resultado, goleadores, asistencias, "
        "tarjetas, jugadores destacados, estadísticas, árbitro, estadio, eventos del partido, "
        "contexto histórico, récords. En ninguna pregunta menciones de dónde sacás los datos.\n"
        "Si un dato no aparece en los datos proporcionados, NO generes una pregunta sobre ese dato.\n"
        "IMPORTANTE: todas las respuestas correctas deben ser 100% verídicas según los datos "
        "proporcionados y corresponder al partido indicado arriba.\n"
        "FORMATO: No uses comillas dobles (\") dentro de los textos de pregunta/opciones/correcta. "
        "Si necesitás citar algo, usá comillas simples (').\n\n"
        "Formato de salida SOLO JSON sin texto adicional ni backticks:\n"
        "{\"preguntas\": [{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}]}"
    )

    # Intentar con Groq primero, luego Gemini como fallback
    raw = None
    intentos = []
    if grok_client:
        intentos.append((grok_client, "llama-3.3-70b-versatile", "Groq"))
    if gemini_client:
        intentos.append((gemini_client, "gemini-2.5-flash", "Gemini"))

    for client, model, nombre in intentos:
        try:
            raw = _llamar_ia(client, model, prompt, nombre)
            break  # Si funcionó, salimos del loop
        except Exception as e:
            print(f"[IA] {nombre} falló — {type(e).__name__}: {e}. {'Intentando con fallback...' if nombre != intentos[-1][2] else 'Sin más opciones.'}")

    if not raw:
        print("[IA] Todas las IAs fallaron, no se pudieron generar preguntas.")
        return []

    texto = raw.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(texto)
    except json.JSONDecodeError:
        import re
        texto_reparado = re.sub(
            r'(?<=[a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ,.\-])"(?=[a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ,.\-])',
            r'\\"',
            texto
        )
        try:
            parsed = json.loads(texto_reparado)
        except json.JSONDecodeError:
            ultimo_corte = texto.rfind("}")
            texto_cortado = texto[:ultimo_corte + 1]
            if not texto_cortado.rstrip().endswith("]}"):
                texto_cortado = texto_cortado.rstrip().rstrip(",") + "]}"
            parsed = json.loads(texto_cortado)

    return parsed.get("preguntas", [])


def generar_preguntas(partido_info: dict, jugadores: list) -> list:
    """
    Genera las preguntas para el partido indicado. La verificacion de si
    hay un partido mas nuevo se hace en /api/mundial-info y al inicio de
    /api/trivias, por lo que aqui no se vuelve a golpear ESPN.
    """
    return _generar_preguntas_ia(partido_info, jugadores)
 
 
# ===============================================================================
#  ENDPOINTS — originales
# ===============================================================================
 
@app.get("/", response_class=HTMLResponse)
async def root():
    ruta_html = os.path.join(DIR, "index.html")
    with open(ruta_html, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
 
 
# ===============================================================================
#  ENDPOINT: ligas disponibles (con Mundial 2026 siempre incluido)
# ===============================================================================

# Ligas "fijas" que siempre aparecen en el menú (independientemente de la API)
LIGAS_FIJAS = [
    {"id": 1,   "nombre": "Mundial 2026", "pais": "🌍 Internacional", "badge": "🏆 MUNDIAL", "es_mundial": True},
    {"id": 732, "nombre": "Mundial 2026", "pais": "🌍 Internacional", "badge": "🏆 MUNDIAL", "es_mundial": True},
]

# IDs de ligas "grandes" que siempre queremos incluir si tienen temporada activa
LIGAS_GRANDES_IDS = {39: "Premier League", 140: "La Liga", 135: "Serie A",
                     78: "Bundesliga", 61: "Ligue 1", 2: "Champions League"}

@app.get("/api/ligas-disponibles")
async def ligas_disponibles():
    """
    Devuelve la lista de ligas disponibles para jugar la trivia.
    - Siempre incluye el Mundial 2026 (probando IDs 1 y 732 en la API).
    - Consulta la API-Football para detectar qué otras ligas tienen
      partidos recientes/en curso. Prueba temporada 2025 y 2026.
    - El campo 'mundial_activo' indica si el Mundial tiene fixtures disponibles.
    """
    ligas_resultado = []
    mundial_activo = False

    if FOOTBALL_API_KEY:
        headers = {
            "x-rapidapi-host": "v3.football.api-sports.io",
            "x-rapidapi-key":  FOOTBALL_API_KEY,
        }

        # ── 1. Verificar cuál ID del Mundial tiene datos ─────────────────────────
        mundial_id_activo = None
        for wid in MUNDIAL_2026_IDS:
            try:
                r = requests.get(
                    f"{API_FOOTBALL_BASE}/fixtures",
                    params={"league": wid, "season": MUNDIAL_2026_SEASON, "last": 1},
                    headers=headers,
                    timeout=6,
                )
                if r.status_code == 200 and r.json().get("response"):
                    mundial_id_activo = wid
                    mundial_activo = True
                    print(f"[LIGAS] ✅ Mundial 2026 activo con league_id={wid}")
                    break
            except Exception as e:
                print(f"[LIGAS] Error verificando mundial id={wid}: {e}")

        # Siempre añadir el Mundial (con el ID que respondió, o 1 por defecto)
        ligas_resultado.append({
            "id":        mundial_id_activo or 1,
            "nombre":    "Mundial 2026",
            "pais":      "🌍 Internacional",
            "badge":     "🔥 EN VIVO" if mundial_activo else "🏆 MUNDIAL",
            "es_mundial": True,
            "activo":    mundial_activo,
        })

        # ── 2. Ligas grandes: probar temporada 2025 primero, luego 2026 ─────────
        # Las ligas europeas (Premier, La Liga, etc.) corren en temporada 2024-25
        # que la API registra como season=2025. El Mundial y otras copas usan 2026.
        TEMPORADAS_A_PROBAR = [2025, 2026, 2024]

        for lid, nombre in LIGAS_GRANDES_IDS.items():
            añadida = False
            for season in TEMPORADAS_A_PROBAR:
                if añadida:
                    break
                try:
                    r = requests.get(
                        f"{API_FOOTBALL_BASE}/fixtures",
                        params={"league": lid, "season": season, "last": 1},
                        headers=headers,
                        timeout=5,
                    )
                    if r.status_code == 200 and r.json().get("response"):
                        fixtures = r.json().get("response", [])
                        # Determinar país desde el primer fixture
                        pais = fixtures[0].get("league", {}).get("country", "Europa") if fixtures else "Europa"
                        ligas_resultado.append({
                            "id":         lid,
                            "nombre":     nombre,
                            "pais":       f"🏴 {pais}",
                            "badge":      "⚽ ACTIVA",
                            "es_mundial": False,
                            "activo":     True,
                            "season":     season,
                        })
                        print(f"[LIGAS] ✅ {nombre} activa en season={season}")
                        añadida = True
                except Exception as e:
                    print(f"[LIGAS] Error verificando {nombre} season={season}: {e}")

        # ── 3. Otras ligas activas detectadas automáticamente ───────────────────
        ids_ya_incluidos = {l["id"] for l in ligas_resultado}
        for season in [2025, 2026]:
            try:
                r = requests.get(
                    f"{API_FOOTBALL_BASE}/leagues",
                    params={"season": season, "current": "true"},
                    headers=headers,
                    timeout=8,
                )
                if r.status_code == 200:
                    for item in r.json().get("response", [])[:40]:
                        ldata = item.get("league", {})
                        cdata = item.get("country", {})
                        lid   = ldata.get("id")
                        if lid and lid not in ids_ya_incluidos:
                            ligas_resultado.append({
                                "id":         lid,
                                "nombre":     ldata.get("name", "Liga"),
                                "pais":       cdata.get("name", ""),
                                "badge":      "⚽",
                                "es_mundial": False,
                                "activo":     True,
                                "season":     season,
                            })
                            ids_ya_incluidos.add(lid)
            except Exception as e:
                print(f"[LIGAS] Error al obtener ligas activas season={season}: {e}")

    else:
        # Sin API key: solo ofrecer el Mundial como opción manual
        ligas_resultado.append({
            "id":     1,
            "nombre": "Mundial 2026",
            "pais":   "🌍 Internacional",
            "badge":  "🏆 MUNDIAL",
            "es_mundial": True,
            "activo": False,
        })

    return {
        "ligas":         ligas_resultado,
        "mundial_activo": mundial_activo,
        "total":         len(ligas_resultado),
    }


# ===============================================================================
#  ENDPOINT: /api/debug-ligas  — para diagnosticar IDs del Mundial en consola
# ===============================================================================
@app.get("/api/debug-ligas")
async def debug_ligas():
    """
    Endpoint de diagnóstico: imprime en consola la respuesta del endpoint
    /leagues de API-Football y devuelve los primeros 20 resultados.
    Útil para confirmar qué ID asigna el proveedor al Mundial 2026.
    """
    if not FOOTBALL_API_KEY:
        return {"error": "FOOTBALL_API_KEY no configurada"}

    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key":  FOOTBALL_API_KEY,
    }

    resultado = {}

    # Probar IDs del Mundial directamente
    for wid in MUNDIAL_2026_IDS:
        try:
            r = requests.get(
                f"{API_FOOTBALL_BASE}/fixtures",
                params={"league": wid, "season": MUNDIAL_2026_SEASON, "last": 3},
                headers=headers,
                timeout=6,
            )
            data = r.json()
            resultado[f"league_{wid}"] = {
                "status_code": r.status_code,
                "total_fixtures": len(data.get("response", [])),
                "sample": data.get("response", [])[:1],
                "errors": data.get("errors"),
            }
            print(f"[DEBUG] league_id={wid}: {r.status_code} | fixtures={len(data.get('response', []))}")
        except Exception as e:
            resultado[f"league_{wid}"] = {"error": str(e)}

    # Listar ligas con "world" en el nombre
    try:
        r2 = requests.get(
            f"{API_FOOTBALL_BASE}/leagues",
            params={"type": "cup", "season": MUNDIAL_2026_SEASON},
            headers=headers,
            timeout=8,
        )
        if r2.status_code == 200:
            todas = r2.json().get("response", [])
            mundiales = [
                {"id": it["league"]["id"], "name": it["league"]["name"], "country": it["country"]["name"]}
                for it in todas
                if "world" in it["league"]["name"].lower() or "fifa" in it["league"]["name"].lower()
            ]
            resultado["ligas_fifa_world"] = mundiales
            print(f"[DEBUG] Ligas FIFA/World encontradas: {mundiales}")
    except Exception as e:
        resultado["ligas_fifa_world_error"] = str(e)

    return resultado


@app.get("/api/mundial-info")
async def mundial_info(liga_id: int = None, season: int = None):
    """
    Devuelve info del partido actual (el "ultimo" del historial).
    - Si se pasa liga_id, busca el último partido de esa liga específica.
    - Si no se pasa, usa el Mundial 2026 (probando IDs 1 y 732).
    SIEMPRE verifica primero contra la API si hay un partido más reciente,
    comparando contra el ultimo partido guardado en el HISTORIAL.
    Si hay uno nuevo, lo agrega al historial marcado como 'ultimo'.
    """
    ultimo_guardado = obtener_ultimo_del_historial()
    # Si se filtró por liga, buscar el "ultimo" de esa liga en el historial
    if liga_id is not None:
        prefijo = f"{liga_id}_"
        candidatos = [p for p in cargar_historial() if p.get("clave", "").startswith(prefijo)]
        if candidatos:
            candidatos.sort(key=lambda p: p.get("guardado_en", ""), reverse=True)
            ultimo_guardado = candidatos[0]
        else:
            ultimo_guardado = {}

    clave_actual = ultimo_guardado.get("clave", "")

    ultimo_partido_espn = obtener_ultimo_partido_api_football(league_id=liga_id, season=season)

    hay_partido_nuevo = bool(
        ultimo_partido_espn and ultimo_partido_espn.get("clave")
        and ultimo_partido_espn.get("clave") != clave_actual
    )

    if hay_partido_nuevo or not ultimo_guardado:
        if ultimo_partido_espn:
            nuevo_partido = ultimo_partido_espn
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
    """
    Devuelve la lista de partidos guardados (para elegir con cuál jugar).
    Cada item incluye clave, descripcion, tipo, si tiene preguntas
    generadas, y si es el 'ultimo' (el más reciente detectado).
    """
    historial = cargar_historial()

    # Ordenar: el ultimo primero, despues por fecha de guardado descendente
    historial_ordenado = sorted(
        historial,
        key=lambda p: (not p.get("ultimo", False), p.get("guardado_en", "")),
    )
    # El sort de arriba pone "ultimo" primero (False < True invertido) pero
    # el resto en orden ascendente de fecha; lo invertimos para el resto:
    ultimos = [p for p in historial_ordenado if p.get("ultimo")]
    resto = [p for p in historial_ordenado if not p.get("ultimo")]
    resto.sort(key=lambda p: p.get("guardado_en", ""), reverse=True)

    partidos = []
    for p in ultimos + resto:
        partidos.append({
            "clave": p.get("clave", ""),
            "descripcion": p.get("descripcion", ""),
            "tipo": p.get("tipo", "finalizado"),
            "ultimo": bool(p.get("ultimo", False)),
            "tiene_preguntas": len(p.get("preguntas", [])) > 0,
            "guardado_en": p.get("guardado_en", ""),
        })

    return {"partidos": partidos, "total": len(partidos)}


@app.get("/api/trivias")
async def obtener_trivias(clave: str = "", refresh: bool = False, liga_id: int = None, season: int = None):
    """
    Devuelve preguntas para jugar.
    - Si se pasa `clave`, busca ese partido en el HISTORIAL y usa/genera
      su propio banco de preguntas (permite jugar partidos anteriores).
    - Si se pasa `liga_id` sin clave, busca el último partido de esa liga.
    - Si no se pasa nada, usa el partido marcado como 'ultimo'.
    - Si no hay historial, consulta la API automáticamente.
    - `refresh=true` fuerza regenerar el banco de ese partido puntual.

    FIX: la clave incluye el league_id (ej: "1_12345") para evitar mezcla
    de preguntas entre ligas distintas.
    """
    if clave and clave != "__reset__":
        partido_item = obtener_partido_historial(clave)
        if not partido_item:
            return {"error": f"Partido con clave '{clave}' no encontrado en el historial"}
    else:
        # Si viene liga_id, buscar el último partido de esa liga en el historial
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
            partido_api = obtener_ultimo_partido_api_football(league_id=liga_id, season=season)
            if partido_api:
                partido_item = upsert_partido_historial(partido_api)
                guardar_partido_rag(partido_api)
                refresh = True
                print(f"[TRIVIAS] Partido obtenido de API: {partido_item.get('clave')}")
            else:
                return {"error": "No hay partido disponible. La API no devolvió datos."}

    banco = [] if refresh else partido_item.get("preguntas", [])
    print(f"[TRIVIAS] Partido: {partido_item.get('clave')} | preguntas en banco: {len(banco)} | refresh: {refresh}")

    if not banco:
        if not grok_client and not gemini_client:
            print("[TRIVIAS] ERROR: no hay ninguna IA configurada")
            return {"error": "No hay IA configurada (GROK_API_KEY ni GEMINI_API_KEY)"}
        try:
            jugadores = []
            fixture_id = partido_item.get("fixture_id")
            print(f"[TRIVIAS] Buscando jugadores para fixture_id: {fixture_id}")
            if fixture_id:
                jugadores = obtener_jugadores_api_football(fixture_id)
                print(f"[TRIVIAS] Jugadores obtenidos: {len(jugadores)}")

            print("[TRIVIAS] Llamando a generar_preguntas...")
            loop = asyncio.get_event_loop()
            banco = await loop.run_in_executor(None, generar_preguntas, partido_item, jugadores)
            print(f"[TRIVIAS] Preguntas generadas: {len(banco)}")
            if banco:
                guardar_preguntas_partido_historial(partido_item.get("clave", ""), banco)
                # Mantener compatibilidad con cache simple si es el ultimo
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
#  ENDPOINTS — salas y resultados  ← NUEVO
# ===============================================================================
 
@app.post("/api/salas/crear")
async def crear_sala(body: CrearSalaRequest):
    """
    Crea una nueva sala de juego.
    Devuelve el código único de la sala para compartir con otros jugadores.
    """
    salas = cargar_salas()
 
    # Generar código único
    codigo = generar_codigo_sala()
    while codigo in salas:
        codigo = generar_codigo_sala()
 
    partido_rag = cargar_partido_rag()
 
    salas[codigo] = {
        "codigo": codigo,
        "creada_en": datetime.utcnow().isoformat(),
        "partido": partido_rag.get("descripcion", ""),
        "clave_partido": partido_rag.get("clave", ""),
        "estado": "esperando",       # esperando | jugando | finalizada
        "jugadores": [
            {
                "nombre": body.nombre_jugador,
                "unido_en": datetime.utcnow().isoformat(),
                "puntaje": None,
                "finalizo": False,
            }
        ],
    }
 
    guardar_salas(salas)
 
    return {
        "codigo_sala": codigo,
        "mensaje": f"Sala {codigo} creada. Compartí este código para que otros se unan.",
        "sala": salas[codigo],
    }
 
 
@app.post("/api/salas/unirse")
async def unirse_sala(body: UnirseRequest):
    """
    Agrega un jugador a una sala existente.
    """
    salas = cargar_salas()
    codigo = body.codigo_sala.upper().strip()
 
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
 
    sala = salas[codigo]
 
    # Verificar que el nombre no esté tomado en esa sala
    nombres_existentes = [j["nombre"].lower() for j in sala["jugadores"]]
    if body.nombre_jugador.lower() in nombres_existentes:
        raise HTTPException(status_code=400, detail=f"El nombre '{body.nombre_jugador}' ya está en uso en esta sala.")
 
    sala["jugadores"].append({
        "nombre": body.nombre_jugador,
        "unido_en": datetime.utcnow().isoformat(),
        "puntaje": None,
        "finalizo": False,
    })
 
    guardar_salas(salas)
 
    return {
        "mensaje": f"¡{body.nombre_jugador} se unió a la sala {codigo}!",
        "sala": sala,
    }
 
 
@app.post("/api/salas/resultado")
async def guardar_resultado(body: GuardarResultadoRequest):
    """
    Guarda el resultado de un jugador al terminar UNA trivia dentro de la sala.
    - Actualiza el "último resultado" del jugador en la sala (para el
      ranking de esa trivia puntual).
    - Persiste la partida en el RAG global de partidas (con el código de
      sala y la clave del partido jugado), para poder calcular el
      RANKING TOTAL de la sala sumando todas las participaciones.
    - La sala NO se cierra: permanece abierta para que se puedan jugar
      más trivias durante todo el Mundial.
    """
    salas = cargar_salas()
    codigo = body.codigo_sala.upper().strip()
 
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
 
    sala = salas[codigo]
 
    # Actualizar puntaje del jugador en la sala (último resultado jugado)
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
        # Si no estaba en la sala (se unió a una sala existente y juega
        # directo), lo agregamos como jugador con su resultado.
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
 
    # -- Persistir en RAG global de partidas (historial de participaciones) --
    partidas = cargar_partidas()
    partidas.append({
        "nombre_jugador":      body.nombre_jugador,
        "puntaje":             body.puntaje,
        "respuestas_correctas": body.respuestas_correctas,
        "total_preguntas":     body.total_preguntas,
        "codigo_sala":         codigo,
        "clave_partido":       body.clave_partido,
        "partido":             sala.get("partido", ""),
        "fecha":               datetime.utcnow().isoformat(),
    })
    guardar_partidas(partidas)
 
    return {
        "mensaje": "Resultado guardado.",
        "sala": sala,
        "todos_terminaron": todos_terminaron,
    }
 
 
@app.get("/api/salas/{codigo}")
async def obtener_sala(codigo: str):
    """
    Devuelve el estado actual de una sala: jugadores, puntajes y leaderboard.
    Útil para polling desde el frontend mientras se espera a que todos terminen.
    """
    salas = cargar_salas()
    codigo = codigo.upper().strip()
 
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
 
    sala = salas[codigo]
 
    # Construir leaderboard ordenado por puntaje (solo los que ya terminaron)
    leaderboard = sorted(
        [j for j in sala["jugadores"] if j["finalizo"]],
        key=lambda x: x["puntaje"],
        reverse=True,
    )
 
    return {
        "sala": sala,
        "leaderboard": leaderboard,
        "jugadores_totales": len(sala["jugadores"]),
        "jugadores_terminaron": sum(1 for j in sala["jugadores"] if j["finalizo"]),
    }
 
 
@app.get("/api/salas/{codigo}/ranking-total")
async def ranking_total_sala(codigo: str):
    """
    Ranking acumulado de TODA la sala: suma los puntajes de todas las
    participaciones (de todas las trivias/partidos jugados) de cada
    jugador en esta sala, durante todo el Mundial.
    """
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
            acumulado[nombre] = {
                "nombre": nombre,
                "puntaje_total": 0,
                "respuestas_correctas_total": 0,
                "total_preguntas_total": 0,
                "partidas_jugadas": 0,
            }
        acumulado[nombre]["puntaje_total"] += p.get("puntaje", 0)
        acumulado[nombre]["respuestas_correctas_total"] += p.get("respuestas_correctas", 0)
        acumulado[nombre]["total_preguntas_total"] += p.get("total_preguntas", 0)
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
    """
    Devuelve el leaderboard global con los mejores puntajes históricos de todas las partidas.
    """
    partidas = cargar_partidas()
 
    # Agrupar por jugador y quedarse con su mejor puntaje
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
    """
    Devuelve todas las partidas jugadas por un jugador específico.
    """
    partidas = cargar_partidas()
    historial = [
        p for p in partidas
        if p["nombre_jugador"].lower() == nombre.lower()
    ]
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
