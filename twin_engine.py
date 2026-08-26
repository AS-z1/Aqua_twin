"""
Coeur du jumeau numerique.

Corrections apportees a la version initiale :
- advance_tick() est desormais reellement appele par l'API (le frontend
  ne faisait qu'un GET snapshot, qui ne fait pas avancer le temps).
- Chargement de fichier .inp simplifie : un seul chemin de secours pour
  l'encodage au lieu de trois blocs try/except redondants et incoherents.
- Suppression du "pip install --force-reinstall" declenche a l'import :
  modifier l'environnement d'execution en cachette au chargement d'un
  module n'est pas un comportement acceptable pour une application
  destinee a etre deployee. Les versions attendues sont documentees
  dans requirements.txt a la place.
- Ajout de get_topology() : coordonnees + connectivite des noeuds/liens,
  necessaires pour dessiner le reseau cote frontend (rien ne l'exposait
  avant).
- Les conduites (pipes) sont desormais controllable=True comme les
  pompes/vannes, pour permettre de vraiment tester des scenarios de
  fermeture de conduite.
- reset_controls() ajoute pour revenir a l'etat nominal sans recharger
  le fichier.
- Verifications explicites (reseau non charge, lien inconnu, temps hors
  bornes) avec des messages clairs plutot que des KeyError bruts.
"""

from __future__ import annotations

import codecs
import copy
import os
import tempfile
import warnings
from typing import Optional

warnings.filterwarnings("ignore")

import wntr
from wntr.network.controls import Control, ControlAction, SimTimeCondition
from wntr.network.base import LinkStatus

# Encodages testes dans l'ordre pour les fichiers .inp qui ne sont pas en UTF-8
# (frequent avec des fichiers exportes depuis des logiciels SIG/EPANET sous Windows)
_FALLBACK_ENCODINGS = (
    "utf-8-sig",
    "windows-1252",
    "latin-1",
    "iso-8859-1",
    "cp850",
)


class TwinEngineError(Exception):
    """Erreur metier du jumeau numerique (a renvoyer telle quelle a l'API)."""


class DigitalTwin:
    """
    Encapsule un reseau EPANET (wntr), sa simulation etendue, et permet
    de rejouer/controler des elements (pompes, vannes, conduites) au fil
    du temps simule.
    """

    def __init__(self) -> None:
        self.wn: Optional[wntr.network.WaterNetworkModel] = None
        self.results = None
        self.timestamps: list[int] = []
        self.tick_index: int = 0
        self._control_counter: int = 0
        self._source_inp_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------
    def load_network(self, inp_path: str) -> None:
        """Charge un reseau EPANET depuis un fichier .inp, gere l'encodage,
        lance la simulation etendue complete, et reinitialise le curseur temporel."""
        if not os.path.exists(inp_path):
            raise TwinEngineError(f"Fichier introuvable : {inp_path}")

        try:
            self.wn = wntr.network.WaterNetworkModel(inp_path)
        except UnicodeDecodeError:
            self.wn = self._load_with_encoding_fallback(inp_path)
        except Exception as e:
            raise TwinEngineError(f"Erreur lors du chargement du reseau : {e}") from e

        self._source_inp_path = inp_path
        self._control_counter = 0
        self._run_full_simulation()
        self.tick_index = 0

    @staticmethod
    def _load_with_encoding_fallback(inp_path: str) -> "wntr.network.WaterNetworkModel":
        last_error: Optional[Exception] = None
        for encoding in _FALLBACK_ENCODINGS:
            try:
                with codecs.open(inp_path, "r", encoding=encoding) as f:
                    content = f.read()
                fd, temp_path = tempfile.mkstemp(suffix=".inp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
                    return wntr.network.WaterNetworkModel(temp_path)
                finally:
                    os.unlink(temp_path)
            except Exception as e:
                last_error = e
                continue
        raise TwinEngineError(
            f"Impossible de lire le fichier .inp (encodages testes : "
            f"{', '.join(_FALLBACK_ENCODINGS)}). Derniere erreur : {last_error}"
        )

    def _run_full_simulation(self) -> None:
        sim = wntr.sim.EpanetSimulator(self.wn)
        try:
            self.results = sim.run_sim()
        except Exception as e:
            raise TwinEngineError(f"Echec de la simulation hydraulique : {e}") from e
        self.timestamps = list(self.results.node["pressure"].index)
        if not self.timestamps:
            raise TwinEngineError("La simulation n'a produit aucun pas de temps.")

    # ------------------------------------------------------------------
    # Etat / proprietes
    # ------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self.wn is not None

    def _require_loaded(self) -> None:
        if not self.loaded:
            raise TwinEngineError("Aucun reseau charge. Chargez un fichier .inp d'abord.")

    @property
    def current_sim_time(self) -> int:
        if not self.timestamps:
            return 0
        return self.timestamps[min(self.tick_index, len(self.timestamps) - 1)]

    @property
    def duration_s(self) -> int:
        return int(self.timestamps[-1]) if self.timestamps else 0

    def is_finished(self) -> bool:
        return self.tick_index >= len(self.timestamps) - 1

    # ------------------------------------------------------------------
    # Avancement du temps
    # ------------------------------------------------------------------
    def advance_tick(self) -> bool:
        """Avance d'un pas de temps. Retourne False si la simulation est deja terminee."""
        self._require_loaded()
        if self.tick_index >= len(self.timestamps) - 1:
            return False
        self.tick_index += 1
        return True

    def set_tick(self, index: int) -> None:
        """Deplace le curseur temporel a un index donne (utilise par le slider frontend)."""
        self._require_loaded()
        if index < 0 or index >= len(self.timestamps):
            raise TwinEngineError(
                f"Index temporel hors bornes : {index} (0 a {len(self.timestamps) - 1})"
            )
        self.tick_index = index

    def reset_time(self) -> None:
        self.tick_index = 0

    # ------------------------------------------------------------------
    # Topologie (pour l'affichage du reseau cote frontend)
    # ------------------------------------------------------------------
    def get_topology(self) -> dict:
        """Coordonnees des noeuds + connectivite des liens, pour dessiner le reseau."""
        self._require_loaded()
        wn = self.wn
        nodes = []
        for name, node in wn.nodes():
            x, y = node.coordinates if node.coordinates else (0.0, 0.0)
            nodes.append({
                "id": name,
                "type": node.node_type.lower(),  # junction | tank | reservoir
                "x": float(x),
                "y": float(y),
            })
        links = []
        for name, link in wn.links():
            links.append({
                "id": name,
                "type": link.link_type.lower(),  # pipe | pump | valve
                "start": link.start_node_name,
                "end": link.end_node_name,
            })
        return {"nodes": nodes, "links": links}

    # ------------------------------------------------------------------
    # Inventaire des composants
    # ------------------------------------------------------------------
    def list_things(self) -> list[dict]:
        self._require_loaded()
        wn = self.wn
        things: list[dict] = []
        for name in wn.junction_name_list:
            n = wn.get_node(name)
            things.append({"thingId": f"junction-{name}", "type": "junction",
                            "attributes": {"elevation": n.elevation}})
        for name in wn.tank_name_list:
            n = wn.get_node(name)
            things.append({"thingId": f"tank-{name}", "type": "tank",
                            "attributes": {"elevation": n.elevation, "diameter": n.diameter}})
        for name in wn.reservoir_name_list:
            things.append({"thingId": f"reservoir-{name}", "type": "reservoir", "attributes": {}})
        for name in wn.pipe_name_list:
            l = wn.get_link(name)
            things.append({"thingId": f"pipe-{name}", "type": "pipe",
                            "attributes": {"length": l.length, "diameter": l.diameter},
                            "controllable": True})
        for name in wn.pump_name_list:
            things.append({"thingId": f"pump-{name}", "type": "pump", "attributes": {},
                            "controllable": True})
        for name in wn.valve_name_list:
            things.append({"thingId": f"valve-{name}", "type": "valve", "attributes": {},
                            "controllable": True})
        return things

    # ------------------------------------------------------------------
    # Instantane (snapshot) de l'etat courant
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        self._require_loaded()
        wn = self.wn
        r = self.results
        t = self.current_sim_time
        pressure, demand, head = r.node["pressure"], r.node["demand"], r.node["head"]
        flow, vel, status = r.link["flowrate"], r.link["velocity"], r.link["status"]

        snap: dict[str, dict] = {}
        for name in wn.junction_name_list:
            snap[f"junction-{name}"] = {
                "pressure_m": round(float(pressure.at[t, name]), 3),
                "demand_m3s": round(float(demand.at[t, name]), 6),
            }
        for name in wn.tank_name_list:
            snap[f"tank-{name}"] = {
                "level_head_m": round(float(head.at[t, name]), 3),
                "pressure_m": round(float(pressure.at[t, name]), 3),
            }
        for name in wn.reservoir_name_list:
            snap[f"reservoir-{name}"] = {"head_m": round(float(head.at[t, name]), 3)}
        for name in wn.pipe_name_list + wn.pump_name_list + wn.valve_name_list:
            snap[self._link_thing_id(name)] = {
                "flowrate_m3s": round(float(flow.at[t, name]), 6),
                "velocity_ms": round(float(vel.at[t, name]), 4) if name in vel.columns else None,
                "status": self._status_label(status.at[t, name]),
            }
        return {
            "sim_time_s": int(t),
            "tick_index": self.tick_index,
            "total_ticks": len(self.timestamps),
            "duration_s": self.duration_s,
            "is_finished": self.is_finished(),
            "things": snap,
        }

    def _link_thing_id(self, name: str) -> str:
        wn = self.wn
        if name in wn.pipe_name_list:
            return f"pipe-{name}"
        if name in wn.pump_name_list:
            return f"pump-{name}"
        return f"valve-{name}"

    @staticmethod
    def _status_label(code) -> str:
        try:
            code = int(code)
        except (ValueError, TypeError):
            return str(code)
        return {0: "Closed", 1: "Open", 2: "Active"}.get(code, str(code))

    # ------------------------------------------------------------------
    # Controle des elements
    # ------------------------------------------------------------------
    def apply_control(self, link_name: str, new_status: str) -> None:
        """Ferme/ouvre un lien a partir de l'instant courant, puis rejoue toute la
        simulation pour propager l'effet. En cas d'echec hydraulique (reseau rendu
        insoluble), la commande est annulee et l'etat precedent est restaure."""
        self._require_loaded()
        if link_name not in self.wn.link_name_list:
            raise TwinEngineError(f"Lien inconnu : {link_name}")

        status_map = {"open": LinkStatus.Open, "closed": LinkStatus.Closed}
        key = new_status.strip().lower()
        if key not in status_map:
            raise TwinEngineError("Statut invalide (attendu : 'open' ou 'closed').")

        backup_wn = copy.deepcopy(self.wn)
        backup_results = self.results
        backup_tick = self.tick_index

        link = self.wn.get_link(link_name)
        self._control_counter += 1
        control_name = f"user_control_{self._control_counter}"
        condition = SimTimeCondition(self.wn, "=", self.current_sim_time)
        action = ControlAction(link, "status", status_map[key])
        self.wn.add_control(control_name, Control(condition, action, name=control_name))

        try:
            self._run_full_simulation()
        except TwinEngineError as e:
            self.wn = backup_wn
            self.results = backup_results
            self.tick_index = backup_tick
            raise TwinEngineError(
                f"Commande refusee : le reseau devient insoluble hydrauliquement "
                f"apres cette action ({e}). Aucune modification appliquee."
            ) from e

        self.tick_index = min(backup_tick, len(self.timestamps) - 1)

    def reset_controls(self) -> None:
        """Recharge le reseau depuis le fichier source, en effacant tous les
        controles utilisateur appliques depuis le chargement."""
        self._require_loaded()
        if not self._source_inp_path:
            raise TwinEngineError("Impossible de reinitialiser : chemin source inconnu.")
        self.load_network(self._source_inp_path)


twin = DigitalTwin()
