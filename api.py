"""
API REST du jumeau numerique.
Expose le moteur (twin_engine.DigitalTwin) sur HTTP pour un frontend web.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from twin_engine import TwinEngineError, twin

app = FastAPI(title="Jumeau Numerique - API", version="1.0")

# CORS ouvert : le frontend est servi sur une autre origine (fichier local
# ou autre port). A restreindre a un domaine precis en production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ControlRequest(BaseModel):
    link_id: str
    status: str  # "open" | "closed"


class TickRequest(BaseModel):
    index: int


def _thing_id_to_link_name(thing_id: str) -> str:
    """'pump-10' -> '10' ; 'pipe-40' -> '40'."""
    for prefix in ("pipe-", "pump-", "valve-"):
        if thing_id.startswith(prefix):
            return thing_id[len(prefix):]
    return thing_id


def _require_loaded():
    if not twin.loaded:
        raise HTTPException(status_code=409, detail="Aucun reseau charge. Utilisez /api/upload d'abord.")


@app.get("/")
def read_root():
    """Sert le frontend (index.html) directement depuis le backend."""
    return FileResponse("index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "network_loaded": twin.loaded}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".inp"):
        raise HTTPException(status_code=400, detail="Le fichier doit avoir l'extension .inp")

    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        twin.load_network(tmp_path)
    except TwinEngineError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {
        "loaded": True,
        "filename": file.filename,
        "duration_s": twin.duration_s,
        "total_ticks": len(twin.timestamps),
    }


@app.get("/api/topology")
def topology():
    _require_loaded()
    try:
        return twin.get_topology()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/things")
def things():
    _require_loaded()
    return twin.list_things()


@app.get("/api/snapshot")
def snapshot():
    _require_loaded()
    return twin.snapshot()


@app.post("/api/advance")
def advance():
    """Avance la simulation d'un pas de temps et renvoie le nouvel instantane."""
    _require_loaded()
    moved = twin.advance_tick()
    return {"advanced": moved, **twin.snapshot()}


@app.post("/api/tick")
def set_tick(req: TickRequest):
    """Deplace directement le curseur temporel (utilise par un slider)."""
    _require_loaded()
    try:
        twin.set_tick(req.index)
    except TwinEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return twin.snapshot()


@app.post("/api/reset-time")
def reset_time():
    _require_loaded()
    twin.reset_time()
    return twin.snapshot()


@app.post("/api/reset-controls")
def reset_controls():
    """Efface toutes les commandes utilisateur et recharge le reseau nominal."""
    _require_loaded()
    try:
        twin.reset_controls()
    except TwinEngineError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return twin.snapshot()


@app.put("/api/control")
def control(req: ControlRequest):
    _require_loaded()
    link_name = _thing_id_to_link_name(req.link_id)
    try:
        twin.apply_control(link_name, req.status)
    except TwinEngineError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return twin.snapshot()