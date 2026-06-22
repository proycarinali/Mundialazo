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


def obtener_datos_partido_por_nombre(nombre_partido: str = None):
    resultado = {"detalles": {"partido": "Buscando partido reciente..."}, "jugadores": []}

    if not SUPABASE_URL or not SUPABASE_KEY:
        resultado["error"] = "Variables SUPABASE_URL o SUPABASE_KEY no configuradas"
        return resultado

    try:
        # Si el frontend envía el nombre del partido, lo buscamos dinámicamente
        if nombre_partido:
            # Tomamos la primera palabra clave del string (ej: "Real Madrid" -> "Real") para mitigar discrepancias
            nombre_limpio = nombre_partido.split(" ")[0] if " " in nombre_partido else nombre_partido
            
            # Intentamos buscar coincidencia en el equipo local
            partidos = supabase_get("partidos", {
                "equipo_local_nombre": f"ilike.*{nombre_limpio}*",
                "limit": 1,
                "select": "*",
            })
            
            # Si no hubo coincidencia, intentamos con el visitante
            if not partidos:
                partidos = supabase_get("partidos", {
                    "equipo_visitante_nombre": f"ilike.*{nombre_limpio}*",
                    "limit": 1,
                    "select": "*",
                })
        else:
            # Fallback histórico: trae el último partido si no se recibe parámetro
            partidos = supabase_get("partidos", {
                "order": "fecha_partido.desc",
                "limit": 1,
                "select": "*",
            })

        if not partidos:
            resultado["detalles"]["partido"] = "No se encontraron partidos coincidentes en la Base de Datos"
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

        # 2. Obtener Jugadores del partido
        jugadores_rows = supabase_get("jugadores_partido", {
            "id_partido": f"eq.{id_partido}",
            "select": "*",
        })

        # 3. Obtener Eventos para calcular estadísticas reales
        eventos_rows = supabase_get("eventos_partido", {
            "id_partido": f"eq.{id_partido}",
            "select": "*",
        })

        stats = {}
        for ev in eventos_rows:
            jid = ev.get("id_jugador")
            if not jid:
                continue
            if jid not in stats:
                stats[jid] = {"goles": 0, "asistencias": 0, "tarjetas_amarillas": 0, "tarjetas_rojas": 0}
            
            tipo = (ev.get("tipo_evento") or "").lower()
            if tipo in ("goal", "gol", "penalty"):
                stats[jid]["goles"] += 1
            elif tipo in ("assist", "asistencia"):
                stats[jid]["asistencias"] += 1
            elif tipo in ("yellowcard", "tarjeta amarilla", "yellow card"):
                stats[jid]["tarjetas_amarillas"] += 1
            elif tipo in ("redcard", "tarjeta roja", "red card"):
                stats[jid]["tarjetas_rojas"] += 1

            # Si el evento identifica un asistente, sumamos su asistencia
            aid = ev.get("id_asistente")
            if aid and tipo in ("goal", "gol", "penalty"):
                if aid not in stats:
                    stats[aid] = {"goles": 0, "asistencias": 0, "tarjetas_amarillas": 0, "tarjetas_rojas": 0}
                stats[aid]["asistencias"] += 1

        # Mapeamos la lista final de jugadores con sus contadores calculados
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
                "tarjetas_rojas": s.get("tarjetas_rojas", 0)
            })

    except Exception as e:
        resultado["error"] = str(e)
        resultado["detalles"]["debug"] = f"Excepcion: {type(e).__name__}: {str(e)}"

    return resultado


@app.get("/")
def home():
    html_content = """
    <html>
        <head><title>Golazo IA Backend</title></head>
        <body style="font-family:sans-serif; padding:40px; background:#0b0f19; color:#f3f4f6;">
            <h1>⚽ Golazo IA - Backend Activo</h1>
            <p>Endpoints disponibles:</p>
            <ul>
                <li><code>/api/trivias?partido_nombre=...</code> - Generar trivia dinámica</li>
                <li><code>/api/test?partido_nombre=...</code> - Probar integraciones</li>
            </ul>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/api/test")
async def probar_apis(partido_nombre: str = None):
    datos = obtener_datos_partido_por_nombre(partido_nombre)
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
async def obtener_trivias(partido_nombre: str = None):
    if not openai_client:
        return {"error": "OPENAI_API_KEY no configurada en las variables de entorno"}

    datos = obtener_datos_partido_por_nombre(partido_nombre)
    
    # Si ocurrió algún error de conexión o consulta en Supabase, lo informamos directamente
    if "error" in datos:
        return {"error": f"Error en origen de datos (Supabase): {datos['error']}"}
        
    info_jugadores = datos.get("jugadores", [])[:15]

    # Caso A: No hay datos de jugadores reales en la BD para este partido específico
    if not info_jugadores:
        if not partido_nombre:
            return {"error": "No se encontraron datos en Supabase y tampoco se especificó un partido_nombre."}
        
        # Le pedimos a la IA que use sus conocimientos globales sobre este partido específico
        prompt_contenido = (
            f"Eres un experto en fútbol. Basándote en tus conocimientos históricos reales del partido '{partido_nombre}', "
            f"crea exactamente 12 preguntas de trivia variadas y desafiantes. "
            f"Incluye detalles sobre goleadores, sustituciones clave, estadísticas del partido o contexto. "
            f"IMPORTANTE: todas las respuestas correctas deben ser 100% verídicas de la realidad de ese partido. "
            f"Formato de salida SOLO JSON sin texto adicional ni backticks: "
            f"{{\"preguntas\": [{{\"\pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}}]}}"
        )
    # Caso B: Sí tenemos los datos estructurados en las tablas relacionales de Supabase
    else:
        prompt_contenido = (
            f"Crea exactamente 12 preguntas de trivia basándote estrictamente en estos jugadores y partido real de la base de datos: "
            f"Partido: {datos['detalles']}. "
            f"Jugadores y estadísticas: {json.dumps(info_jugadores, ensure_ascii=False)}. "
            f"Formato de salida SOLO JSON sin texto adicional ni backticks: "
            f"{{\"preguntas\": [{{\"\pregunta\": \"...\", \"opciones\": [\"A\",\"B\",\"C\"], \"correcta\": \"...\"}}]}}"
        )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_contenido}],
            max_tokens=2500
        )
        raw_text = response.choices[0].message.content
        texto = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto)
    except Exception as e:
        return {"error": f"Error procesando la respuesta o estructura JSON de OpenAI: {str(e)}"}
