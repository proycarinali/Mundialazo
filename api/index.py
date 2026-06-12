import sys
import asyncio
import json
import os
import random
import string
import requests
from datetime import datetime
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
 
GROK_API_KEY = os.environ.get("GROK_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
 
# ─── Archivos RAG ────────────────────────────────────────────────────────────
DIR = os.path.dirname(__file__)
CACHE_FILE    = os.path.join(DIR, "preguntas_cache.json")
PARTIDO_FILE  = os.path.join(DIR, "partido_cache.json")
SALAS_FILE    = os.path.join(DIR, "salas_cache.json")       # ← NUEVO
PARTIDAS_FILE = os.path.join(DIR, "partidas_cache.json")    # ← NUEVO
 
# ─── Cliente Groq ─────────────────────────────────────────────────────────────
grok_client = None
if GROK_API_KEY:
    grok_client = OpenAI(
        api_key=GROK_API_KEY,
        base_url="https://api.groq.com/openai/v1"
    )
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  MODELOS Pydantic
# ═══════════════════════════════════════════════════════════════════════════════
 
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
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS RAG — partido y preguntas (sin cambios)
# ═══════════════════════════════════════════════════════════════════════════════
 
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
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS RAG — salas y partidas  ← NUEVO
# ═══════════════════════════════════════════════════════════════════════════════
 
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
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  FOOTBALL API
# ═══════════════════════════════════════════════════════════════════════════════
 
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
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  FOOTBALL API: último partido jugado del Mundial 2026
# ═══════════════════════════════════════════════════════════════════════════════

MUNDIAL_2026_LEAGUE_ID = 1   # (legado, ya no se usa con ESPN)
MUNDIAL_2026_SEASON    = 2026

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_SUMMARY_URL    = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary"


import requests
from datetime import datetime

def obtener_ultimo_partido_mundial2026() -> dict:
    try:
        # 1. Definir rango de fechas dinámico para no perder partidos de ayer
        fecha_inicio = "20260611"
        fecha_hoy = datetime.now().strftime("%Y%m%d")
        url = f"{ESPN_SCOREBOARD_URL}?dates={fecha_inicio}-{fecha_hoy}&limit=100"

        res = requests.get(url, timeout=6)
        if res.status_code != 200:
            return {}

        data = res.json()
        eventos = data.get("events", [])
        if not eventos:
            return {}

        finalizados = []
        for ev in eventos:
            status = ev.get("status", {}).get("type", {})
            # Agregamos validación por nombre de estado por seguridad
            if status.get("completed") is True or status.get("state") == "post" or status.get("name") == "STATUS_FINAL":
                finalizados.append(ev)

        if not finalizados:
            return {}

        # 2. Ordenar usando datetime real para evitar fallas de ordenamiento de strings
        def mapear_fecha(e):
            try:
                return datetime.fromisoformat(e.get("date", "").replace("Z", "+00:00"))
            except ValueError:
                return datetime.min

        finalizados.sort(key=mapear_fecha, reverse=True)
        evento = finalizados[0]

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
            "tipo": "finalizado",
            "contexto": contexto,
        }
    except Exception:
        return {}

# ═══════════════════════════════════════════════════════════════════════════════
#  IA: detectar partido del mundial
# ═══════════════════════════════════════════════════════════════════════════════
 
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
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  IA: generar 50 preguntas de trivia
# ═══════════════════════════════════════════════════════════════════════════════
def _generar_preguntas_ia(partido_info: dict, jugadores: list) -> list:
    if not grok_client:
        return []
 
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
            f"\n\nEstadísticas reales de jugadores del partido:\n{json.dumps(jugadores_compactos, ensure_ascii=False)}"
        )
    else:
        contexto_jugadores = ""
 
    estado = "en curso" if tipo == "en_curso" else "ya finalizado"
 
    prompt = (
        "A continuación tenés datos OFICIALES extraídos en tiempo real desde la API de ESPN "
        f"sobre un partido ({estado}) del Mundial 2026. Estos son los ÚNICOS datos válidos: "
        "no uses tu conocimiento previo sobre otros partidos, no asumas otro resultado, "
        "y no inventes jugadores, goles ni estadísticas que no estén en este texto.\n\n"
        f"DATOS DEL PARTIDO (fuente: ESPN):\n{contexto_partido}"
        f"{contexto_jugadores}\n\n"
        "Basándote ESTRICTAMENTE en los datos de ESPN anteriores, crea EXACTAMENTE 10 preguntas "
        "de trivia variadas y desafiantes. Incluye preguntas sobre: resultado, goleadores, asistencias, "
        "tarjetas, jugadores destacados, estadísticas, árbitro, estadio, eventos del partido, "
        "contexto histórico, récords. En ninguna pregunta nombres que los datos los sacas de ESPN."
        "Si un dato no aparece en los datos de ESPN, NO generes una pregunta sobre ese dato.\n"
        "IMPORTANTE: todas las respuestas correctas deben ser 100% verídicas según los datos de ESPN "
        "proporcionados y corresponder al partido indicado arriba.\n"
        "FORMATO: No uses comillas dobles (\") dentro de los textos de pregunta/opciones/correcta. "
        "Si necesitás citar algo, usá comillas simples (').\n\n"
        "Formato de salida SOLO JSON sin texto adicional ni backticks:\n"
        "{\"preguntas\": [{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}]}"
    )
 
    response = grok_client.chat.completions.create(
        model = "llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=7000,
        timeout=45,
    )
    raw = response.choices[0].message.content
    texto = raw.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(texto)
    except json.JSONDecodeError:
        # Intento de reparacion: escapar comillas dobles "sueltas" dentro de
        # los valores de string que rompen el JSON (comunes en modelos chicos).
        import re
        texto_reparado = re.sub(
            r'(?<=[a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ,.\-])"(?=[a-zA-Z0-9áéíóúÁÉÍÓÚñÑ ,.\-])',
            r'\\"',
            texto
        )
        try:
            parsed = json.loads(texto_reparado)
        except json.JSONDecodeError:
            # Ultimo recurso: cortar en el ultimo "}" valido del array de preguntas
            ultimo_corte = texto.rfind("}")
            texto_cortado = texto[:ultimo_corte + 1]
            # cerrar array y objeto principal
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
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS — originales
# ═══════════════════════════════════════════════════════════════════════════════
 
@app.get("/", response_class=HTMLResponse)
async def root():
    ruta_html = os.path.join(DIR, "index.html")
    with open(ruta_html, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
 
 
@app.get("/api/mundial-info")
async def mundial_info():
    """
    Devuelve info del partido actual. SIEMPRE verifica primero contra ESPN
    si hay un partido finalizado mas reciente; si lo hay, actualiza el RAG
    de partido y borra el banco de preguntas viejo (para que /api/trivias
    regenere las preguntas para el partido nuevo).
    Si no hay partido nuevo, simplemente devuelve lo guardado en cache.
    """
    partido_rag = cargar_partido_rag()
    clave_rag = partido_rag.get("clave", "")

    ultimo_partido = obtener_ultimo_partido_mundial2026()

    hay_partido_nuevo = bool(
        ultimo_partido and ultimo_partido.get("clave") and ultimo_partido.get("clave") != clave_rag
    )

    if hay_partido_nuevo or not partido_rag:
        partido_rag = ultimo_partido if ultimo_partido else (
            partido_rag if partido_rag else detectar_partido_mundial_con_ia()
        )
        guardar_partido_rag(partido_rag)

        if hay_partido_nuevo and os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)

        return {**partido_rag, "desde_cache": False, "partido_nuevo_detectado": hay_partido_nuevo}

    return {**partido_rag, "desde_cache": True, "partido_nuevo_detectado": False}
 
 
@app.get("/api/trivias")
async def obtener_trivias(clave: str = "", refresh: bool = False):
    """
    Lee el partido actual del RAG (ya actualizado por /api/mundial-info,
    que es quien chequea ESPN) y devuelve preguntas. Si la clave pedida
    no coincide con la del RAG, o no hay banco cacheado, regenera.
    """
    partido_rag = cargar_partido_rag()
    clave_rag = partido_rag.get("clave", "")

    if not partido_rag:
        partido_rag = detectar_partido_mundial_con_ia()
        guardar_partido_rag(partido_rag)
        clave_rag = partido_rag.get("clave", "")
        refresh = True

    # Si pidieron una clave distinta a la del RAG actual, forzar regeneracion
    if clave and clave != clave_rag:
        refresh = True

    banco = [] if refresh else cargar_preguntas_rag()

    if not banco:
        if not grok_client:
            return {"error": "GROK_API_KEY no configurada"}
        try:
            jugadores = []
            fixture_id = partido_rag.get("fixture_id")
            if fixture_id:
                jugadores = obtener_jugadores_fixture(fixture_id)

            loop = asyncio.get_event_loop()
            banco = await loop.run_in_executor(None, generar_preguntas, partido_rag, jugadores)
            if banco:
                guardar_preguntas_rag(banco)
            else:
                return {"error": "No se pudieron generar preguntas"}
        except Exception as e:
            return {"error": str(e)}

    muestra = random.sample(banco, min(10, len(banco)))
    return {
        "preguntas": muestra,
        "total_banco": len(banco),
        "partido": partido_rag.get("descripcion", ""),
        "tipo": partido_rag.get("tipo", "finalizado"),
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
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS — salas y resultados  ← NUEVO
# ═══════════════════════════════════════════════════════════════════════════════
 
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
 
    if sala["estado"] == "finalizada":
        raise HTTPException(status_code=400, detail="La sala ya finalizó.")
 
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
    Guarda el resultado de un jugador al terminar su partida.
    - Actualiza la sala con el puntaje del jugador.
    - Persiste la partida en el RAG global de partidas.
    - Si todos los jugadores terminaron, marca la sala como 'finalizada'.
    """
    salas = cargar_salas()
    codigo = body.codigo_sala.upper().strip()
 
    if codigo not in salas:
        raise HTTPException(status_code=404, detail=f"Sala '{codigo}' no encontrada.")
 
    sala = salas[codigo]
 
    # Actualizar puntaje del jugador en la sala
    jugador_encontrado = False
    for jugador in sala["jugadores"]:
        if jugador["nombre"].lower() == body.nombre_jugador.lower():
            jugador["puntaje"] = body.puntaje
            jugador["respuestas_correctas"] = body.respuestas_correctas
            jugador["total_preguntas"] = body.total_preguntas
            jugador["finalizo"] = True
            jugador["finalizado_en"] = datetime.utcnow().isoformat()
            jugador_encontrado = True
            break
 
    if not jugador_encontrado:
        raise HTTPException(status_code=404, detail=f"Jugador '{body.nombre_jugador}' no está en la sala.")
 
    # Si todos finalizaron → cerrar sala
    todos_terminaron = all(j["finalizo"] for j in sala["jugadores"])
    if todos_terminaron:
        sala["estado"] = "finalizada"
        sala["finalizada_en"] = datetime.utcnow().isoformat()
 
    guardar_salas(salas)
 
    # ── Persistir en RAG global de partidas ──────────────────────────────────
    partidas = cargar_partidas()
    partidas.append({
        "nombre_jugador":      body.nombre_jugador,
        "puntaje":             body.puntaje,
        "respuestas_correctas": body.respuestas_correctas,
        "total_preguntas":     body.total_preguntas,
        "codigo_sala":         codigo,
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
