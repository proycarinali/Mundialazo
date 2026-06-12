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
 
def obtener_jugadores_fixture(fixture_id: int) -> list:
    headers = {
        "x-apisports-key": FOOTBALL_API_KEY or "",
        "x-rapidapi-host": "v3.football.api-sports.io"
    }
    jugadores = []
    try:
        res = requests.get(
            f"https://v3.football.api-sports.io/fixtures/players?fixture={fixture_id}",
            headers=headers, timeout=10
        )
        if res.status_code == 200:
            data = res.json()
            for team in data.get("response", []):
                for player in team.get("players", []):
                    stats = player.get("statistics", [{}])[0]
                    jugadores.append({
                        "nombre":            player["player"]["name"],
                        "posicion":          stats.get("games", {}).get("position", "N/A"),
                        "minutos":           stats.get("games", {}).get("minutes", 0),
                        "calificacion":      stats.get("games", {}).get("rating", "N/A"),
                        "goles":             stats.get("goals", {}).get("total", 0),
                        "asistencias":       stats.get("goals", {}).get("assists", 0),
                        "tiros_total":       stats.get("shots", {}).get("total", 0),
                        "tiros_al_arco":     stats.get("shots", {}).get("on", 0),
                        "pases_completados": stats.get("passes", {}).get("accuracy", "0%"),
                        "faltas_cometidas":  stats.get("fouls", {}).get("committed", 0),
                        "faltas_recibidas":  stats.get("fouls", {}).get("drawn", 0),
                        "tarjetas_amarillas":stats.get("cards", {}).get("yellow", 0),
                        "tarjetas_rojas":    stats.get("cards", {}).get("red", 0),
                        "atajadas":          stats.get("goalkeeper", {}).get("saves", 0),
                    })
    except Exception:
        pass
    return jugadores
 
 
# ═══════════════════════════════════════════════════════════════════════════════
#  FOOTBALL API: último partido jugado del Mundial 2026
# ═══════════════════════════════════════════════════════════════════════════════
 
MUNDIAL_2026_LEAGUE_ID = 1   # ID de "World Cup" en api-football
MUNDIAL_2026_SEASON    = 2026
 
 
def obtener_ultimo_partido_mundial2026() -> dict:
    """
    Consulta api-football y devuelve info del último fixture finalizado
    del Mundial 2026. Devuelve {} si no hay datos o falla.
    """
    if not FOOTBALL_API_KEY:
        return {}
 
    headers = {
        "x-apisports-key": FOOTBALL_API_KEY,
        "x-rapidapi-host": "v3.football.api-sports.io"
    }
 
    try:
        res = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers=headers,
            params={
                "league": MUNDIAL_2026_LEAGUE_ID,
                "season": MUNDIAL_2026_SEASON,
                "status": "FT",
                "last": 1,
            },
            timeout=10
        )
        if res.status_code != 200:
            return {}
 
        data = res.json()
        if not data.get("response"):
            return {}
 
        fixture = data["response"][0]
 
        # Validar que el fixture realmente pertenezca al Mundial 2026
        liga_id = fixture.get("league", {}).get("id")
        liga_nombre = (fixture.get("league", {}).get("name") or "").lower()
        if liga_id != MUNDIAL_2026_LEAGUE_ID and "world cup" not in liga_nombre:
            return {}
        fixture_id = fixture["fixture"]["id"]
        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        goles_home = fixture["goals"]["home"]
        goles_away = fixture["goals"]["away"]
        fecha = fixture["fixture"]["date"]
        estadio = fixture["fixture"]["venue"].get("name", "")
        arbitro = fixture["fixture"].get("referee", "")
        ronda = fixture["league"].get("round", "")
 
        descripcion = f"{ronda}: {home} {goles_home}-{goles_away} {away}"
 
        contexto = (
            f"{descripcion}. Fecha: {fecha}. Estadio: {estadio}. "
            f"Árbitro: {arbitro}. Ronda: {ronda}."
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
            model="llama-3.1-8b-instant",
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
 
def generar_preguntas(partido_info: dict, jugadores: list) -> list:
    if not grok_client:
        return []
 
    contexto_partido = partido_info.get("contexto", partido_info.get("descripcion", ""))
    tipo = partido_info.get("tipo", "finalizado")
 
    if jugadores:
        contexto_jugadores = (
            f"\n\nEstadísticas reales de jugadores del partido:\n{json.dumps(jugadores[:15], ensure_ascii=False)}"
        )
    else:
        contexto_jugadores = ""
 
    estado = "en curso" if tipo == "en_curso" else "ya finalizado"
 
    prompt = (
        f"Eres un experto en fútbol. El partido de referencia es ({estado}):\n"
        f"{contexto_partido}"
        f"{contexto_jugadores}\n\n"
        "Basándote ESTRICTAMENTE en esa información, crea EXACTAMENTE 50 preguntas de trivia "
        "variadas y desafiantes. Incluye preguntas sobre: goles y sus minutos, asistencias, "
        "sustituciones, tarjetas, penales, jugadores destacados, estadísticas, árbitro, estadio, "
        "contexto histórico, récords. "
        "IMPORTANTE: todas las respuestas correctas deben ser 100% verídicas y corresponder al partido indicado.\n\n"
        "Formato de salida SOLO JSON sin texto adicional ni backticks:\n"
        "{\"preguntas\": [{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}]}"
    )
 
    response = grok_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096
    )
    raw = response.choices[0].message.content
    texto = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(texto)
    return parsed.get("preguntas", [])
 
 
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
    partido_rag = cargar_partido_rag()
    if partido_rag:
        return {**partido_rag, "desde_cache": True}
    partido = detectar_partido_mundial_con_ia()
    guardar_partido_rag(partido)
    return {**partido, "desde_cache": False}
 
 
@app.get("/api/trivias")
async def obtener_trivias(clave: str = "", refresh: bool = False):
    partido_rag = cargar_partido_rag()
    clave_rag = partido_rag.get("clave", "")
 
    # 1) Obtener el ultimo partido jugado del Mundial 2026 via api-football
    ultimo_partido = obtener_ultimo_partido_mundial2026()
 
    # 2) Si la API no devolvio nada, usar lo que haya en cache (o detectar con IA)
    if not ultimo_partido:
        ultimo_partido = partido_rag if partido_rag else detectar_partido_mundial_con_ia()
 
    # 3) Si pidieron una clave especifica distinta a la del cache, limpiar
    if clave and clave_rag and clave != clave_rag:
        limpiar_rag()
        partido_rag = {}
        clave_rag = ""
 
    # 4) Hay un partido nuevo (distinta clave) respecto al guardado en RAG?
    hay_partido_nuevo = bool(
        ultimo_partido.get("clave") and ultimo_partido.get("clave") != clave_rag
    )
 
    if hay_partido_nuevo or not partido_rag:
        partido_rag = ultimo_partido
        guardar_partido_rag(partido_rag)
        refresh = True  # forzar regeneracion del banco de preguntas
 
    banco = [] if refresh else cargar_preguntas_rag()
 
    if not banco:
        if not grok_client:
            return {"error": "GROK_API_KEY no configurada"}
        try:
            jugadores = []
            fixture_id = partido_rag.get("fixture_id")
            if FOOTBALL_API_KEY and fixture_id:
                jugadores = obtener_jugadores_fixture(fixture_id)
 
            banco = generar_preguntas(partido_rag, jugadores)
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
        "partido_nuevo_detectado": hay_partido_nuevo,
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
