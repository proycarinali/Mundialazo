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
FOOTBALL_API_KEY = os.environ.get("BALDONLITE")
 
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
        base_url="https://groq.com"
    )

# --- Cliente Gemini (fallback) ------------------------------------------------
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = OpenAI(
        api_key=GEMINI_API_KEY,
        base_url="https://googleapis.com"
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
 
API_FOOTBALL_BASE    = "https://api-sports.io"
MUNDIAL_2026_IDS    = []
MUNDIAL_2026_SEASON = 2026


# ===============================================================================
#  LÓGICA AUTOMÁTICA EN SEGUNDO PLANO (Verificación cada 4 horas)
# ===============================================================================

def _obtener_datos_ultimo_partido_liga() -> dict:
    """
    Consulta la API externa usando tu clave BALDONLITE (FOOTBALL_API_KEY).
    """
    url = f"{API_FOOTBALL_BASE}/fixtures?live=all"
    headers = {
        "x-rapidapi-key": FOOTBALL_API_KEY,
        "x-apisports-key": FOOTBALL_API_KEY
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            res_json = response.json()
            fixtures = res_json.get("response", [])
            if fixtures:
                f = fixtures[0]
                return {
                    "clave": f"partido_{f['fixture']['id']}",
                    "descripcion": f"{f['teams']['home']['name']} vs {f['teams']['away']['name']}",
                    "tipo": f['fixture']['status']['short'],
                    "contexto": f"Partido de {f['league']['name']}. Marcador actual: {f['goals']['home']}-{f['goals']['away']}.",
                    "fixture_id": f['fixture']['id'],
                    "liga_id": f['league']['id'],
                    "liga_nombre": f['league']['name'],
                    "season": f['league']['season']
                }
    except Exception as e:
        print(f"❌ Error al consultar el último partido desde la API: {e}")
    return {}


def _generar_trivia_desde_llm(partido_info: dict) -> list:
    """
    Genera el bloque de preguntas competitivas usando Grok o Gemini.
    """
    client = grok_client or gemini_client
    if not client:
        print("⚠️ No hay ningún cliente LLM configurado en el entorno.")
        return []

    prompt = (
        f"Generá una trivia de al menos 10 preguntas competitivas con sus opciones múltiples "
        f"basada estrictamente en el siguiente encuentro futbolístico: {partido_info['descripcion']}.\n"
        f"Contexto adicional: {partido_info['contexto']}.\n"
        f"Devolvé la respuesta en un formato estructurado JSON plano que sea una lista de objetos válidos."
    )
    
    try:
        model_name = "grok-beta" if grok_client else "gemini-1.5-flash"
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = response.choices.message.content
        return json.loads(texto)
    except Exception as e:
        print(f"❌ Error al estructurar la trivia mediante el LLM: {e}")
        return []


async def verificar_y_actualizar_trivia_loop():
    """
    Loop continuo no bloqueante de 4 horas usando las utilidades RAG del archivo.
    """
    await asyncio.sleep(2)
    print("🚀 Tarea de verificación periódica de trivias activada (cada 4 horas).")
    
    while True:
        try:
            ultimo_partido = _obtener_datos_ultimo_partido_liga()
            if ultimo_partido and "clave" in ultimo_partido:
                partido_actual_rag = cargar_partido_rag()
                
                if ultimo_partido.get("clave") != partido_actual_rag.get("clave"):
                    print(f"✨ Nuevo partido detectado: {ultimo_partido['descripcion']}. Actualizando RAG...")
                    
                    # Se limpian los archivos locales viejos
                    limpiar_rag()
                    
                    # Se generan preguntas sobre el último partido de la liga
                    nuevas_preguntas = _generar_trivia_desde_llm(ultimo_partido)
                    
                    if nuevas_preguntas:
                        guardar_partido_rag(ultimo_partido)
                        guardar_preguntas_rag(nuevas_preguntas)
                        upsert_partido_historial(ultimo_partido, preguntas=nuevas_preguntas)
                        print("✅ Trivia regenerada correctamente en el RAG. Datos anteriores purgados.")
                    else:
                        print("⚠️ La IA no retornó preguntas válidas. Reintento en el próximo bloque.")
                else:
                    print("💤 El último partido de la liga no ha variado. No se realizan cambios en el RAG.")
            else:
                print("⚠️ No se recibieron datos de partidos en esta iteración.")
        except Exception as e:
            print(f"❌ Error crítico en el loop de actualización: {e}")
            
        await asyncio.sleep(4 * 3600)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(verificar_y_actualizar_trivia_loop())
