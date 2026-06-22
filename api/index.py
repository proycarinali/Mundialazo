import json
import os
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)


def supabase_get(tabla: str, params: dict) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{tabla}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    res = requests.get(url, headers=headers, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def obtener_datos_final_mundo():
    resultado = {"detalles": {"partido": "Buscando partido reciente..."}, "jugadores": []}

    if not SUPABASE_URL or not SUPABASE_KEY:
        resultado["error"] = "Variables SUPABASE_URL o SUPABASE_KEY no configuradas"
        return resultado

    try:
        # 1. Último partido
        partidos = supabase_get("partidos", {
            "order": "fecha_partido.desc",
            "limit": 1,
            "select": "*",
        })

        if not partidos:
            resultado["detalles"]["partido"] = "No se encontraron partidos"
            return resultado

        partido = partidos[0]
        id_partido = partido["id_partido"]
        resultado["detalles"]["partido"] = (
            f"{partido['equipo_local_nombre']} {partido['equipo_local_goles']} "
            f"- {partido['equipo_visitante_goles']} {partido['equipo_visitante_nombre']}"
        )
        resultado["detalles"]["liga"] = partido.get("liga_nombre", "N/A")
        resultado["detalles"]["fecha"] = str(partido.get("fecha_partido", "N/A"))
        resultado["detalles"]["ganador"] = partido.get("ganador", "N/A")
        resultado["detalles"]["tanda_penales"] = partido.get("tanda_penales", False)

        # 2. Jugadores del partido
        jugadores_rows = supabase_get("jugadores_partido", {
            "id_partido": f"eq.{id_partido}",
            "select": "*",
        })

        # 3. Eventos del partido
        eventos_rows = supabase_get("eventos_partido", {
            "id_partido": f"eq.{id_partido}",
            "select": "*",
        })

        # Calcular estadísticas por jugador a partir de eventos
        stats: dict = {}

        for ev in eventos_rows:
            jid = ev.get("id_jugador")
            if not jid:
                continue
            if jid not in stats:
                stats[jid] = {
                    "goles": 0,
                    "asistencias": 0,
                    "tarjetas_amarillas": 0,
                    "tarjetas_rojas": 0,
                }
            tipo = (ev.get("tipo_evento") or "").lower()
            if tipo in ("goal", "gol", "penalty"):
                stats[jid]["goles"] += 1
            elif tipo in ("assist", "asistencia"):
                stats[jid]["asistencias"] += 1
            elif tipo in ("yellowcard", "tarjeta amarilla", "yellow card"):
                stats[jid]["tarjetas_amarillas"] += 1
            elif tipo in ("redcard", "tarjeta roja", "red card"):
                stats[jid]["tarjetas_rojas"] += 1

            # Asistente dentro del mismo evento de gol
            aid = ev.get("id_asistente")
            if aid and tipo in ("goal", "gol", "penalty"):
                if aid not in stats:
                    stats[aid] = {
                        "goles": 0,
                        "asistencias": 0,
                        "tarjetas_amarillas": 0,
                        "tarjetas_rojas": 0,
                    }
                stats[aid]["asistencias"] += 1

        # 4. Armar lista de jugadores con sus stats
        for jug in jugadores_rows:
            jid = jug.get("id_jugador", "")
            s = stats.get(jid, {})
            resultado["jugadores"].append({
                "nombre": jug.get("nombre_jugador", "N/A"),
                "posicion": jug.get("posicion", "N/A"),
                "titular": jug.get("titular", True),
                "equipo_id": jug.get("id_equipo", "N/A"),
                "goles": s.get("goles", 0),
                "asistencias": s.get("asistencias", 0),
                "tarjetas_amarillas": s.get("tarjetas_amarillas", 0),
                "tarjetas_rojas": s.get("tarjetas_rojas", 0),
                "minutos": None,
                "calificacion": None,
                "tiros_total": None,
                "tiros_al_arco": None,
                "pases_completados": None,
                "faltas_cometidas": None,
                "faltas_recibidas": None,
                "atajadas": None,
            })

    except Exception as e:
        resultado["error"] = str(e)
        resultado["detalles"]["debug"] = f"Excepcion: {type(e).__name__}: {str(e)}"

    return resultado


@app.get("/", response_class=HTMLResponse)
async def root():
    ruta_html = os.path.join(os.path.dirname(__file__), "index.html")
    with open(ruta_html, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/test")
async def probar_apis():
    datos = obtener_datos_final_mundo()
    openai_res = {"status": "No configurado"}

    if openai_client:
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Hola"}]
            )
            openai_res = {"status": 200, "body": response.choices[0].message.content}
        except Exception as e:
            openai_res = {"error": str(e)}

    return {"openai": openai_res, "datos_futbol": datos}


@app.get("/api/trivias")
async def obtener_trivias():
    if not openai_client:
        return {"error": "OPENAI_API_KEY no configurada"}

    datos = obtener_datos_final_mundo()
    info_jugadores = datos.get("jugadores", [])[:15]

    if not info_jugadores:
        prompt_contenido = (
            "Eres un experto en futbol. Basandote en tus conocimientos sobre el ultimo partido "
            "del ultimo Mundial, o del Mundial en curso si lo hay, "
            "crea 50 preguntas de trivia variadas y desafiantes. "
            "Puedes incluir preguntas sobre: goleadores (Mbappe hat-trick, Di Maria, Messi penales), "
            "sustituciones clave, minutos de los goles, jugadores destacados, estadisticas del partido, "
            "arbitro, asistencias, tarjetas, penales (quien pateo, quien atajo), "
            "contexto historico (primera copa de Messi, records rotos, etc.). "
            "IMPORTANTE: todas las respuestas correctas deben ser 100% veridicas. "
            "Formato de salida SOLO JSON sin texto adicional ni backticks: "
            "{\"preguntas\": [{\"pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}]}"
        )
    else:
        prompt_contenido = (
            f"Crea 50 preguntas de trivia basandote estrictamente en estos jugadores y partido: "
            f"Partido: {datos['detalles']}. "
            f"Jugadores y estadisticas: {json.dumps(info_jugadores, ensure_ascii=False)}. "
            f"Formato: {{\"preguntas\": [{{\"pregunta\": \"...\", "
            f"\"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}}]}}"
        )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_contenido}],
            max_tokens=4096
        )
        raw_text = response.choices[0].message.content
        texto = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto)
    except Exception as e:
        return {"error": str(e)}
