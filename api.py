"""
API REST du jumeau numerique — Aqua Twin v2.

Expose le moteur (twin_engine.DigitalTwin) sur HTTP pour un frontend web.
v2 — ajouts :
- Gestion de sessions multi-utilisateurs (chacune son propre moteur WNTR).
- Endpoint /api/history pour les graphes d'evolution.
- Endpoint /api/settings (parametres serveur).
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from twin_engine import DigitalTwin, TwinEngineError

app = FastAPI(title="Jumeau Numerique - API", version="2.0")

# CORS ouvert : le frontend est servi sur une autre origine.
# En production, restreindre a un domaine precis si besoin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------
# Gestion des sessions : chaque session possede son propre moteur WNTR.
# ----------------------------------------------------------------------
_SESSIONS: dict[str, DigitalTwin] = {}
_SESSION_LOCK = threading.Lock()
_DEFAULT_SESSION_ID = "default"
# Duree de vie maximale d'une session sans activite (secondes) : 30 min.
SESSION_TTL = 30 * 60


def _get_session(session_id: str) -> DigitalTwin:
    """Retourne la session existante ou en cree une nouvelle."""
    sid = session_id or _DEFAULT_SESSION_ID
    with _SESSION_LOCK:
        twin = _SESSIONS.get(sid)
        if twin is None:
            twin = DigitalTwin()
            _SESSIONS[sid] = twin
        return twin


def _cleanup_sessions() -> int:
    """Supprime les sessions inactives depuis plus de SESSION_TTL secondes."""
    now = time.time()
    stale = [
        sid for sid, t in _SESSIONS.items()
        if t.loaded and (now - t._loaded_at) > SESSION_TTL
    ]
    with _SESSION_LOCK:
        for sid in stale:
            _SESSIONS.pop(sid, None)
    return len(stale)


class ControlRequest(BaseModel):
    link_id: str
    status: str  # "open" | "closed"


class TickRequest(BaseModel):
    index: int


class LeakRequest(BaseModel):
    link_id: str  # ex: "pipe-10"
    magnitude: float = 0.05


class ScenarioRequest(BaseModel):
    name: str  # id du scenario (cf. twin_engine.list_scenarios)


class PressureDrivenRequest(BaseModel):
    enabled: bool = True


def _thing_id_to_link_name(thing_id: str) -> str:
    """'pump-10' -> '10' ; 'pipe-40' -> '40'."""
    for prefix in ("pipe-", "pump-", "valve-"):
        if thing_id.startswith(prefix):
            return thing_id[len(prefix):]
    return thing_id


def _element_parse(thing_id: str) -> tuple[str, str]:
    """'junction-10' -> ('node', '10') ; 'pipe-40' -> ('link', '40')."""
    if thing_id.startswith(("junction-", "tank-", "reservoir-")):
        return ("node", thing_id.split("-", 1)[1])
    return ("link", thing_id.split("-", 1)[1])


@app.get("/")
def read_root():
    """Sert le frontend (index.html) directement depuis le backend."""
    return FileResponse("index.html")


@app.get("/api/health")
def health(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    _cleanup_sessions()
    return {"status": "ok", "network_loaded": twin.loaded}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...),
                 x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    if not file.filename.lower().endswith(".inp"):
        raise HTTPException(status_code=400, detail="Le fichier doit avoir l'extension .inp")
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    twin = _get_session(x_session_id)
    try:
        twin.load_network(tmp_path, file.filename)
    except TwinEngineError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "loaded": True,
        "filename": file.filename,
        "duration_s": twin.duration_s,
        "total_ticks": len(twin.timestamps),
        "session_id": x_session_id or _DEFAULT_SESSION_ID,
    }


@app.get("/api/topology")
def topology(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        return twin.get_topology()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/things")
def things(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    return twin.list_things()


@app.get("/api/snapshot")
def snapshot(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    return twin.snapshot()


@app.post("/api/advance")
def advance(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    moved = twin.advance_tick()
    return {"advanced": moved, **twin.snapshot()}


@app.post("/api/tick")
def set_tick(req: TickRequest, x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        twin.set_tick(req.index)
    except TwinEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return twin.snapshot()


@app.post("/api/reset-time")
def reset_time(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    twin.reset_time()
    return twin.snapshot()


@app.post("/api/reset-controls")
def reset_controls(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        twin.reset_controls()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return twin.snapshot()


@app.put("/api/control")
def control(req: ControlRequest, x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    link_name = _thing_id_to_link_name(req.link_id)
    try:
        twin.apply_control(link_name, req.status)
    except TwinEngineError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return twin.snapshot()


# ======================================================================
#  Fondations metier : fuites, scenarios, demande dependante de la pression
# ======================================================================
@app.post("/api/leak")
def leak(req: LeakRequest, x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Applique une fuite (casse) sur une conduite."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    link_name = _thing_id_to_link_name(req.link_id)
    try:
        twin.apply_leak(link_name, req.magnitude)
    except TwinEngineError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {**twin.snapshot(), "leak": {"link": link_name,
                                        "magnitude": req.magnitude}}


@app.post("/api/clear-leaks")
def clear_leaks(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Recharge le reseau source pour effacer fuites et scenarios."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        twin.clear_leaks()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return twin.snapshot()


@app.get("/api/scenarios")
def scenarios(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Liste les scenarios d'exploitation disponibles."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    return {"scenarios": twin.list_scenarios()}


@app.post("/api/scenario")
def run_scenario(req: ScenarioRequest,
                 x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Applique un scenario metier et rejoue la simulation."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        res = twin.run_scenario(req.name)
    except TwinEngineError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {**res, "snapshot": twin.snapshot()}


@app.post("/api/pressure-driven")
def pressure_driven(req: PressureDrivenRequest,
                    x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Active/desactive la demande dependante de la pression (PDA)."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        res = twin.set_pressure_driven(req.enabled)
    except TwinEngineError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return res


# ======================================================================
#  Detection de fuites
# ======================================================================
@app.post("/api/detect-leaks")
def detect_leaks(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Localise une fuite par correlation des residus de pression."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        result = twin.run_leak_detection()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return result


# ======================================================================
#  Optimisation & indicateurs
# ======================================================================
@app.get("/api/optimization")
def optimization(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Indicateurs d'optimisation (pression, energie, zones, ILI, vitesse)."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        return twin.get_optimization()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ======================================================================
#  Qualite de l'eau
# ======================================================================
@app.post("/api/quality")
def quality(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Simule la qualite de l'eau (chlore + age) et produit l'alerte."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    try:
        return twin.run_quality_analysis()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history/{thing_id}")
def history(thing_id: str, x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Historique temporel complet d'un element (pour les graphes d'evolution)."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    element_type, element_id = _element_parse(thing_id)
    try:
        data = twin.get_history(element_type, element_id)
    except TwinEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"thing_id": thing_id, "series": data}


@app.get("/api/network-history")
def network_history(x_session_id: str = Header(default=_DEFAULT_SESSION_ID)):
    """Historique global du reseau (pression moyenne, debit total) sur tous les pas."""
    twin = _get_session(x_session_id)
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge.")
    return twin.get_network_history()


@app.get("/api/settings")
def settings():
    """Parametres exposees au frontend (utile pour la page Parametres)."""
    cleaned = _cleanup_sessions()
    return {
        "version": "2.0",
        "engine": "WNTR 1.5 / EPANET",
        "active_sessions": len(_SESSIONS),
        "session_ttl_s": SESSION_TTL,
        "sessions_cleaned": cleaned,
    }
