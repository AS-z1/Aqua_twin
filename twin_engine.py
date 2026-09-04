"""
Coeur du jumeau numerique — Aqua Twin v2.

Moteur hydraulique base sur WNTR/EPANET, gere la simulation etendue,
le controle d'elements (pompes, vannes, conduites), et l'extraction
d'historiques temporels pour les graphes d'evolution.

v2 — ajouts :
- get_history() : extraction de l'historique complet d'un element
  (pression, debit, vitesse) sur tous les pas de temps.
- list_sessions / sessions multicompte gerees cote API.
"""

from __future__ import annotations

import codecs
import copy
import os
import tempfile
import time
import warnings
from typing import Optional

warnings.filterwarnings("ignore")

import wntr
from wntr.network.controls import Control, ControlAction, SimTimeCondition
from wntr.network.base import LinkStatus

_FALLBACK_ENCODINGS = (
    "utf-8-sig",
    "windows-1252",
    "latin-1",
    "iso-8859-1",
    "cp850",
)


class TwinEngineError(Exception):
    """Erreur metier du jumeau numerique."""


class DigitalTwin:
    """
    Encapsule un reseau EPANET (wntr), sa simulation etendue, et permet
    de rejouer/controler des elements au fil du temps simule.
    """

    def __init__(self) -> None:
        self.wn: Optional[wntr.network.WaterNetworkModel] = None
        self.results = None
        self.timestamps: list[int] = []
        self.tick_index: int = 0
        self._control_counter: int = 0
        self._source_inp_path: Optional[str] = None
        self._filename: str = ""
        self._loaded_at: float = 0.0
        # Demande dependante de la pression (PDA) : desactive par defaut.
        self._pda: bool = False

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------
    def load_network(self, inp_path: str, filename: str = "") -> None:
        if not os.path.exists(inp_path):
            raise TwinEngineError(f"Fichier introuvable : {inp_path}")
        try:
            self.wn = wntr.network.WaterNetworkModel(inp_path)
        except UnicodeDecodeError:
            self.wn = self._load_with_encoding_fallback(inp_path)
        except Exception as e:
            raise TwinEngineError(f"Erreur lors du chargement du reseau : {e}") from e
        self._source_inp_path = inp_path
        self._filename = filename or os.path.basename(inp_path)
        self._control_counter = 0
        self._run_full_simulation()
        self.tick_index = 0
        self._loaded_at = time.time()

    @staticmethod
    def _load_with_encoding_fallback(inp_path: str):
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

    @staticmethod
    def _simulate(wn, pda: bool = False):
        """Lance une simulation sur un modele donne (le modele courant ou
        une copie temporaire) et renvoie les resultats WNTR."""
        if pda:
            # Demande dependante de la pression : simulateur natif WNTR.
            try:
                wn.options.hydraulic.demand_model = "PDA"
                wn.options.hydraulic.required_pressure = 15.0
                wn.options.hydraulic.minimum_pressure = 0.0
            except Exception:
                pass
            sim = wntr.sim.WNTRSimulator(wn)
        else:
            sim = wntr.sim.EpanetSimulator(wn)
        return sim.run_sim()

    def _run_full_simulation(self) -> None:
        try:
            self.results = self._simulate(self.wn, getattr(self, "_pda", False))
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
            raise TwinEngineError("Aucun reseau charge.")

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
        self._require_loaded()
        if self.tick_index >= len(self.timestamps) - 1:
            return False
        self.tick_index += 1
        return True

    def set_tick(self, index: int) -> None:
        self._require_loaded()
        if index < 0 or index >= len(self.timestamps):
            raise TwinEngineError(
                f"Index temporel hors bornes : {index} (0 a {len(self.timestamps) - 1})"
            )
        self.tick_index = index

    def reset_time(self) -> None:
        self.tick_index = 0

    # ------------------------------------------------------------------
    # Topologie
    # ------------------------------------------------------------------
    def get_topology(self) -> dict:
        self._require_loaded()
        wn = self.wn
        nodes = []
        for name, node in wn.nodes():
            x, y = node.coordinates if node.coordinates else (0.0, 0.0)
            nodes.append({
                "id": name,
                "type": node.node_type.lower(),
                "x": float(x),
                "y": float(y),
            })
        links = []
        for name, link in wn.links():
            links.append({
                "id": name,
                "type": link.link_type.lower(),
                "start": link.start_node_name,
                "end": link.end_node_name,
            })
        return {"nodes": nodes, "links": links}

    # ------------------------------------------------------------------
    # Inventaire
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
    # Snapshot
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
    # Historique temporel (pour les graphes)
    # ------------------------------------------------------------------
    def get_history(self, element_type: str, element_id: str) -> list[dict]:
        """
        Retourne l'evolution complete d'un element sur tous les pas de temps.
        element_type : 'node' (junction/tank/reservoir) ou 'link' (pipe/pump/valve)
        element_id   : nom technique de l'element (ex: '10', 'Lake', '100')
        """
        self._require_loaded()
        r = self.results
        timestamps = self.timestamps
        if not timestamps:
            return []

        # Determiner le type reel pour extraire les bonnes colonnes
        wn = self.wn
        is_junction = element_id in wn.junction_name_list
        is_tank = element_id in wn.tank_name_list
        is_reservoir = element_id in wn.reservoir_name_list
        is_pipe = element_id in wn.pipe_name_list
        is_pump = element_id in wn.pump_name_list
        is_valve = element_id in wn.valve_name_list
        is_node = is_junction or is_tank or is_reservoir
        is_link = is_pipe or is_pump or is_valve

        if not is_node and not is_link:
            raise TwinEngineError(f"Element inconnu : {element_id}")

        history = []
        if is_node:
            pressure_df = r.node["pressure"]
            demand_df = r.node["demand"]
            head_df = r.node["head"]
            for i, t in enumerate(timestamps):
                entry = {"tick": i, "time_s": int(t)}
                if is_junction:
                    entry["pressure_m"] = round(float(pressure_df.at[t, element_id]), 3)
                    entry["demand_m3s"] = round(float(demand_df.at[t, element_id]), 6)
                elif is_tank:
                    entry["level_head_m"] = round(float(head_df.at[t, element_id]), 3)
                    entry["pressure_m"] = round(float(pressure_df.at[t, element_id]), 3)
                elif is_reservoir:
                    entry["head_m"] = round(float(head_df.at[t, element_id]), 3)
                history.append(entry)
        elif is_link:
            flow_df = r.link["flowrate"]
            vel_df = r.link["velocity"]
            status_df = r.link["status"]
            for i, t in enumerate(timestamps):
                entry = {
                    "tick": i,
                    "time_s": int(t),
                    "flowrate_m3s": round(float(flow_df.at[t, element_id]), 6),
                    "velocity_ms": round(float(vel_df.at[t, element_id]), 4) if element_id in vel_df.columns else None,
                    "status": self._status_label(status_df.at[t, element_id]),
                }
                history.append(entry)
        return history

    def get_network_history(self) -> dict:
        """
        Historique global du reseau : pression moyenne aux jonctions,
        debit total et nombre de liens ouverts, pour chaque pas de temps.
        Utile pour le graphe "indicateur reseau".
        """
        self._require_loaded()
        r = self.results
        wn = self.wn
        pressure_df = r.node["pressure"]
        flow_df = r.link["flowrate"]
        status_df = r.link["status"]
        junctions = wn.junction_name_list
        open_links = wn.pipe_name_list + wn.pump_name_list + wn.valve_name_list

        history = []
        for i, t in enumerate(self.timestamps):
            if junctions:
                avg_p = float(pressure_df.loc[t, junctions].mean())
            else:
                avg_p = 0.0
            total_flow = 0.0
            open_count = 0
            for name in open_links:
                f = float(flow_df.at[t, name])
                s = int(status_df.at[t, name])
                if s in (1, 2):
                    total_flow += abs(f)
                    open_count += 1
            history.append({
                "tick": i,
                "time_s": int(t),
                "avg_pressure_m": round(avg_p, 3),
                "total_flow_m3s": round(total_flow, 6),
                "open_links": open_count,
                "total_links": len(open_links),
            })
        return {"series": history}

    # ------------------------------------------------------------------
    # Controle
    # ------------------------------------------------------------------
    def apply_control(self, link_name: str, new_status: str) -> None:
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
                f"Commande refusee : le reseau devient insoluble "
                f"apres cette action ({e}). Aucune modification appliquee."
            ) from e
        self.tick_index = min(backup_tick, len(self.timestamps) - 1)

    def reset_controls(self) -> None:
        self._require_loaded()
        if not self._source_inp_path:
            raise TwinEngineError("Impossible de reinitialiser : chemin source inconnu.")
        self.load_network(self._source_inp_path, self._filename)

    # ==================================================================
    #  FONDATIONS METIER (Phase 1)
    #  Mode fuite, scenarios d'exploitation, demande dependante de la
    #  pression. Toute modification est jouee sur une copie et restauree
    #  en cas d'echec, pour ne jamais casser un reseau en cours d'usage.
    # ==================================================================

    @staticmethod
    def _apply_leak_to(wn, link_name: str, magnitude: float) -> None:
        """Applique une fuite (casse) sur `link_name` d'un modele WNTR.

        Modele realiste d'emetteur EPANET :
        - on isole la conduite (plus d'ecoulement a travers la casse) ;
        - on place un emetteur a une extremite : le debit de fuite est
          proportionnel a la pression locale, Q = C.p^0.5.
        """
        link = wn.get_link(link_name)
        link.initial_status = LinkStatus.Closed
        end = link.end_node_name
        if end in wn.junction_name_list:
            wn.get_node(end).emitter_coefficient = max(float(magnitude), 1e-6)
        elif link.start_node_name in wn.junction_name_list:
            wn.get_node(link.start_node_name).emitter_coefficient = max(float(magnitude), 1e-6)

    def _morph_link_leak(self, link_name: str, magnitude: float) -> None:
        """Ferme la conduite et injecte une fuite (emetteur) sur le modele
        courant. Le debit de fuite depend de la pression (Q = C.p^0.5)."""
        if link_name not in self.wn.link_name_list:
            raise TwinEngineError(f"Conduite inconnue : {link_name}")
        self._apply_leak_to(self.wn, link_name, magnitude)

    def apply_leak(self, link_name: str, magnitude: float = 0.05) -> dict:
        """Simule une fuite (casse) sur une conduite et rejoue la simulation.

        `magnitude` : coefficient d'emetteur (force de la fuite). Retourne
        un descriptif de la fuite appliquee. En cas d'echec, l'etat
        precedent est integralement restaure.
        """
        self._require_loaded()
        if link_name not in self.wn.pipe_name_list:
            raise TwinEngineError("Une fuite ne peut s'appliquer que sur une conduite (pipe).")
        backup_wn = copy.deepcopy(self.wn)
        backup_results = self.results
        backup_tick = self.tick_index
        try:
            self._morph_link_leak(link_name, magnitude)
            self._run_full_simulation()
        except TwinEngineError as e:
            self.wn, self.results, self.tick_index = backup_wn, backup_results, backup_tick
            raise TwinEngineError(f"Fuite refusee : le reseau devient insoluble ({e}).") from e
        self.tick_index = min(backup_tick, len(self.timestamps) - 1)
        return {"link": link_name, "magnitude": magnitude, "leak_active": True}

    def clear_leaks(self) -> dict:
        """Recharge le reseau source pour effacer toute fuite/scenario."""
        self._require_loaded()
        if not self._source_inp_path:
            raise TwinEngineError("Impossible : chemin source inconnu.")
        self.load_network(self._source_inp_path, self._filename)
        return {"cleared": True}

    # ------------------------------------------------------------------
    # Scenarios metier
    # ------------------------------------------------------------------
    def list_scenarios(self) -> list[dict]:
        """Decrit les scenarios d'exploitation disponibles."""
        return [
            {"id": "cassee_conduite", "label": "Casse de conduite",
             "desc": "Fermeture d'une conduite principale + fuite emetteur."},
            {"id": "panne_pompe", "label": "Panne de pompe",
             "desc": "Arret d'une pompe de relevage ou de surpression."},
            {"id": "pic_demande", "label": "Pic de demande",
             "desc": "Surcharge de +40% sur la demande de tous les usagers."},
            {"id": "fermeture_vanne", "label": "Fermeture d'une vanne",
             "desc": "Isolation d'une maille par fermeture de vanne."},
        ]

    def run_scenario(self, scenario_id: str) -> dict:
        """Applique un scenario metier et rejoue la simulation.

        Toute action est realisee sur une copie et restauree si le reseau
        devient insoluble. Renvoie un descriptif de ce qui a ete modifie.
        """
        self._require_loaded()
        backup_wn = copy.deepcopy(self.wn)
        backup_results = self.results
        backup_tick = self.tick_index
        applied: list[str] = []

        try:
            if scenario_id == "cassee_conduite":
                pipes = self.wn.pipe_name_list
                if not pipes:
                    raise TwinEngineError("Aucune conduite dans le reseau.")
                # On vise la conduite la plus longue (souvent un axe principal).
                target = max(pipes, key=lambda p: self.wn.get_link(p).length or 0.0)
                self._morph_link_leak(target, 0.06)
                applied.append(f"casse sur {target}")
            elif scenario_id == "panne_pompe":
                pumps = self.wn.pump_name_list
                if not pumps:
                    raise TwinEngineError("Aucune pompe dans le reseau.")
                target = pumps[0]
                self.wn.get_link(target).initial_status = LinkStatus.Closed
                applied.append(f"panne pompe {target}")
            elif scenario_id == "pic_demande":
                factor = 1.4
                for name in self.wn.junction_name_list:
                    node = self.wn.get_node(name)
                    try:
                        base = node.demand_timeseries_list[0].base_value
                        node.demand_timeseries_list[0].base_value = base * factor
                    except Exception:
                        continue
                applied.append("demande +40% sur toutes les jonctions")
            elif scenario_id == "fermeture_vanne":
                valves = self.wn.valve_name_list
                if valves:
                    self.wn.get_link(valves[0]).initial_status = LinkStatus.Closed
                    applied.append(f"vanne {valves[0]} fermee")
                else:
                    raise TwinEngineError("Aucune vanne dans le reseau.")
            else:
                raise TwinEngineError(f"Scenario inconnu : {scenario_id}")

            self._run_full_simulation()
        except TwinEngineError as e:
            self.wn, self.results, self.tick_index = backup_wn, backup_results, backup_tick
            raise TwinEngineError(f"Scenario refuse : {e}") from e
        self.tick_index = min(backup_tick, len(self.timestamps) - 1)
        return {"scenario": scenario_id, "applied": applied, "active": True}

    # ------------------------------------------------------------------
    # Demande dependante de la pression (PDA)
    # ------------------------------------------------------------------
    def set_pressure_driven(self, enabled: bool = True) -> dict:
        """Active/desactive la demande dependante de la pression.

        En mode PDA, la consommation diminue quand la pression chute
        (plus realiste en periode de fuite ou de penurie). WNTR propose un
        simulateur natif PDA ; en cas d'echec on revient sans bruit au
        mode demande constante (comportement historique).
        """
        self._require_loaded()
        self._pda = bool(enabled)
        backup_wn = copy.deepcopy(self.wn)
        backup_results = self.results
        backup_tick = self.tick_index
        try:
            self._run_full_simulation()
        except TwinEngineError:
            self._pda = False
            self.wn, self.results, self.tick_index = backup_wn, backup_results, backup_tick
            raise TwinEngineError("Demande dependante de la pression indisponible pour ce reseau.")
        self.tick_index = min(backup_tick, len(self.timestamps) - 1)
        return {"pressure_driven": self._pda}

    # ==================================================================
    #  DETECTION DE FUITES (Phase 2)
    #  Generation de scenarios de fuite, residus de pression, correlation
    #  de signatures (base non-ML, extensible au ML) et heatmap d'alarme.
    # ==================================================================
    def run_leak_detection(self, max_candidates: int = 18) -> dict:
        """Localise une fuite par correlation des residus de pression.

        Methode reelle, sans entrainement ML :
        1. Simulation de reference (sans fuite) -> pressions nominales P0.
        2. Capteurs : sous-echantillon de jonctions (telemetrie limitee).
        3. Pour chaque conduite candidate, on simule une fuite unitaire et
           on enregistre la signature (variation de pression aux capteurs).
        4. On compare chaque signature a l'ecart observe (si une fuite a
           ete appliquee) par correlation de Pearson -> classement.

        Le nombre de candidats est borne (`max_candidates`) pour rester
        interactif ; en production cette analyse est precalculee hors-ligne.
        """
        self._require_loaded()
        wn = self.wn
        junctions = wn.junction_name_list
        pipes = wn.pipe_name_list
        empty = {"method": "residual-correlation", "candidates": [],
                 "heatmap": {}, "observations": [], "note": "Reseau trop petit."}
        if not junctions or len(pipes) < 2:
            return empty

        # --- 1. Reference nominale : reseau SANS fuite -------------------
        # On part d'une copie propre (emetteurs annules, conduites ouvertes)
        # pour obtenir les pressions de reference P0.
        t_ref = self.timestamps[-1]
        nominal = copy.deepcopy(wn)
        for n in nominal.junction_name_list:
            nominal.get_node(n).emitter_coefficient = 0.0
        for p in nominal.pipe_name_list:
            nominal.get_link(p).initial_status = LinkStatus.Open
        try:
            nom_res = self._simulate(nominal)
            base_pressures = {
                n: float(nom_res.node["pressure"].at[t_ref, n])
                for n in junctions
            }
        except Exception:
            return empty

        # --- 2. Choix des capteurs (sous-echantillon realiste) ----------
        step = max(1, int(len(junctions) / max(6, int(len(junctions) * 0.45))))
        sensors = junctions[::step][: max(8, int(len(junctions) * 0.4))]
        if not sensors:
            sensors = junctions[:8]

        # --- 3. Observations : fuite actuellement appliquee sur le reseau
        # L'ecart nominal - courant aux capteurs est la "mesure telemetrie".
        observations = [base_pressures[s] - float(
            self.results.node["pressure"].at[t_ref, s]) for s in sensors]

        # --- 4. Signatures de fuite pour chaque conduite candidate -------
        candidates = pipes[:max_candidates]
        signatures: dict[str, list[float]] = {}
        for ln in candidates:
            sig = self._leak_signature(ln, sensors, t_ref, base_pressures, nominal)
            if sig is not None:
                signatures[ln] = sig

        # --- 5. Correlation et classement -------------------------------
        ranked = self._rank_signatures(signatures, observations, base_pressures, sensors)
        heatmap = self._build_heatmap(signatures, sensors, base_pressures)

        return {
            "method": "residual-correlation",
            "n_candidates": len(signatures),
            "n_sensors": len(sensors),
            "candidates": ranked,
            "heatmap": heatmap,
            "sensors": sensors,
            "observations": [round(v, 3) for v in observations],
        }

    def _leak_signature(self, link_name, sensors, t_ref, base_pressures, nominal):
        """Simule une fuite sur `link_name` (a partir du reseau nominal) et
        renvoie la variation de pression observee aux capteurs, ou None si
        la conduite ne converge pas en regime de fuite."""
        try:
            tmp = copy.deepcopy(nominal)
            self._apply_leak_to(tmp, link_name, 0.05)
            res = self._simulate(tmp)
            return [base_pressures[s] - float(res.node["pressure"].at[t_ref, s])
                    for s in sensors]
        except Exception:
            return None

    @staticmethod
    def _pearson(a, b):
        """Coefficient de correlation de Pearson entre deux series."""
        n = len(a)
        if n == 0:
            return 0.0
        ma, mb = sum(a) / n, sum(b) / n
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        va = sum((x - ma) ** 2 for x in a)
        vb = sum((y - mb) ** 2 for y in b)
        if va == 0 or vb == 0:
            return 0.0
        return cov / (va * vb) ** 0.5

    def _rank_signatures(self, signatures, observations, base_pressures, sensors):
        """Classe les conduites par ressemblance a la fuite observee."""
        ranked = []
        for ln, sig in signatures.items():
            score = self._pearson(sig, observations)
            max_drop = max(sig) if sig else 0.0
            avg_drop = sum(sig) / len(sig) if sig else 0.0
            ranked.append({
                "link": ln,
                "correlation": round(score, 3),
                "max_drop_m": round(max_drop, 2),
                "avg_drop_m": round(avg_drop, 2),
            })
        ranked.sort(key=lambda r: r["correlation"], reverse=True)
        return ranked

    def _build_heatmap(self, signatures, sensors, base_pressures):
        """Heatmap : pour chaque capteur, l'ecart de pression maximum
        observe parmi toutes les conduites candidates (zone d'alarme)."""
        heat = {}
        for s in sensors:
            vals = [sig[sensors.index(s)] for sig in signatures.values() if sig]
            heat[s] = round(max(vals) if vals else 0.0, 2)
        return heat

    # ==================================================================
    #  OPTIMISATION (Phase 4)
    #  Pression / PRV, energie de pompage, zones (DMA simplifie), ILI/NRF,
    #  conformite de la vitesse dans la plage reglementaire [0.5, 1.5].
    # ==================================================================
    def get_optimization(self) -> dict:
        """Indicateurs d'optimisation du reseau au pas de temps courant."""
        self._require_loaded()
        wn = self.wn
        r = self.results
        t = self.current_sim_time
        junctions = wn.junction_name_list

        pressure_df = r.node["pressure"]
        demand_df = r.node["demand"]
        head_df = r.node["head"]
        flow_df = r.link["flowrate"]
        vel_df = r.link["velocity"]

        # --- Pression aux jonctions ------------------------------------
        pressures = [float(pressure_df.at[t, n]) for n in junctions] if junctions else [0.0]
        p_min = min(pressures) if pressures else 0.0
        p_avg = sum(pressures) / len(pressures) if pressures else 0.0
        p_max = max(pressures) if pressures else 0.0
        # Service minimal usuel : 20 m (2 bars). Nœuds sous ce seuil = deficit.
        min_service = 20.0
        deficit_nodes = [n for n, p in zip(junctions, pressures) if p < min_service]

        # --- PRV / recommandation de gestion de pression ---------------
        # Si la pression moyenne est nettement superieure au besoin, une
        # reduction (PRV ou vanne de regulation) economise de l'eau perdue.
        recommandation = None
        if pressures and p_avg > 45.0:
            target = max(30.0, p_avg - 15.0)
            recommandation = {
                "type": "pressure_reduction",
                "message": (f"Pression moyenne elevee ({p_avg:.0f} m). Une "
                            f"regulee a ~{target:.0f} m (PRV) reduirait les "
                            f"pertes (fuite ~ p^1.5) et l'usure."),
                "target_m": round(target, 1),
                "saving_estimate_pct": round(100 - 100 * (target / p_avg) ** 1.5, 1),
            }

        # --- Energie de pompage -----------------------------------------
        energy_kwh, energy_cost = self._pumping_energy()
        # Cout typique (indicatif) : 0.12 EUR / kWh.
        COST_PER_KWH = 0.12
        energy_cost = round(energy_kwh * COST_PER_KWH, 2)

        # --- Zones (DMA simplifie) --------------------------------------
        zones = self._compute_zones()

        # --- Vitesse : conformite dans [0.5, 1.5] m/s -------------------
        velocity_status, velocity_ok_count, velocity_count = self._velocity_conformity()

        # --- Bilan hydraulique / NRW (eau non facturee) -----------------
        produced, consumed = self._water_balance(t)
        nrf = (produced - consumed) / produced if produced > 0 else 0.0

        # --- ILI indicatif (Infrastructure Leakage Index) ---------------
        ilif = self._indicative_ili(t)

        return {
            "pressure": {"min_m": round(p_min, 2), "avg_m": round(p_avg, 2),
                         "max_m": round(p_max, 2), "min_service_m": min_service,
                         "deficit_nodes": deficit_nodes,
                         "deficit_count": len(deficit_nodes)},
            "prv_recommendation": recommandation,
            "energy": {"kwh": round(energy_kwh, 3), "cost_eur": energy_cost,
                       "cost_per_kwh": COST_PER_KWH},
            "zones": zones,
            "velocity": velocity_status,
            "velocity_summary": {"ok_count": velocity_ok_count,
                                 "total": velocity_count,
                                 "ok_pct": round(100 * velocity_ok_count / velocity_count, 1)
                                 if velocity_count else 0.0},
            "nrw": {"produced_m3": round(produced, 3), "consumed_m3": round(consumed, 3),
                    "nrf_pct": round(100 * nrf, 1)},
            "ili": ilif,
            "pressure_driven": getattr(self, "_pda", False),
        }

    def _pumping_energy(self) -> tuple[float, float]:
        """Estime l'energie consommee par les pompes sur toute la duree.

        P = rho.g.Q.H / eta  (puissance instantanee) integree sur le temps.
        Retourne (kWh, cout_eur)."""
        wn = self.wn
        r = self.results
        flow_df = r.link["flowrate"]
        head_df = r.node["head"]
        status_df = r.link["status"]
        pumps = wn.pump_name_list
        if not pumps:
            return 0.0, 0.0
        rho, g, eta = 1000.0, 9.81, 0.75
        total_j = 0.0
        dt = 0.0
        for i, t in enumerate(self.timestamps):
            if i == 0:
                continue
            dt = (t - self.timestamps[i - 1]) or 0.0
            for p in pumps:
                s = int(status_df.at[t, p])
                if s == 0:  # pompe arretee (fermee)
                    continue
                q = float(flow_df.at[t, p])
                h_start = float(head_df.at[t, wn.get_link(p).start_node_name])
                h_end = float(head_df.at[t, wn.get_link(p).end_node_name])
                h = max(0.0, h_start - h_end)
                total_j += rho * g * abs(q) * h / eta * dt
        return total_j / 3.6e6, 0.0  # joules -> kWh

    def _compute_zones(self) -> list[dict]:
        """Zones de desserte (DMA simplifie) : partition des jonctions par
        source d'alimentation (BFS depuis reservoirs/cules). En l'absence
        de donnees de sectorisation, on groupe par bande de pression."""
        wn = self.wn
        r = self.results
        t = self.current_sim_time
        sources = wn.reservoir_name_list or wn.tank_name_list
        zones = []
        if not sources:
            return zones
        # Carte d'adjacence entre nœuds.
        adj: dict[str, set] = {}
        for _name, link in wn.links():
            sn, en = link.start_node_name, link.end_node_name
            adj.setdefault(sn, set()).add(en)
            adj.setdefault(en, set()).add(sn)
        # BFS depuis chaque source pour attribuer les nœuds.
        assigned: dict[str, str] = {}
        for src in sources:
            frontier = [src]
            assigned[src] = src
            while frontier:
                cur = frontier.pop()
                for nb in adj.get(cur, ()):
                    if nb not in assigned:
                        assigned[nb] = src
                        frontier.append(nb)
        # Statistiques par zone.
        by_zone: dict[str, dict] = {}
        for n, z in assigned.items():
            bucket = by_zone.setdefault(z, {"nodes": 0, "p_sum": 0.0, "d_sum": 0.0})
            bucket["nodes"] += 1
            if n in wn.junction_name_list:
                bucket["p_sum"] += float(r.node["pressure"].at[t, n])
                bucket["d_sum"] += float(r.node["demand"].at[t, n])
        for z, b in by_zone.items():
            zones.append({
                "zone": z,
                "nodes": b["nodes"],
                "avg_pressure_m": round(b["p_sum"] / max(1, b["nodes"]), 2),
                "total_demand_m3s": round(b["d_sum"], 6),
            })
        return zones

    def _velocity_conformity(self) -> tuple[dict, int, int]:
        """Conformite de la vitesse dans la plage reglementaire [0.5, 1.5].

        - < 0.5 m/s : risque de depot / stagnation (eau qui dort).
        - 0.5-1.5   : plage de fonctionnement optimal.
        - > 1.5 m/s : risque d'erosion, de bruit et de surpression.
        Retourne (status_par_conduite, nb_ok, nb_total)."""
        wn = self.wn
        r = self.results
        t = self.current_sim_time
        vel_df = r.link["velocity"]
        status_df = r.link["status"]
        V_MIN, V_MAX = 0.5, 1.5
        status: dict[str, str] = {}
        ok = total = 0
        for name in wn.pipe_name_list:
            total += 1
            if name not in vel_df.columns:
                status[name] = "na"
                continue
            v = float(vel_df.at[t, name])
            s = int(status_df.at[t, name])
            if s == 0:  # conduite fermee
                status[name] = "closed"
                continue
            if v < V_MIN:
                status[name] = "low"
            elif v > V_MAX:
                status[name] = "high"
            else:
                status[name] = "ok"
                ok += 1
        return status, ok, total

    def _water_balance(self, t) -> tuple[float, float]:
        """Bilan : volume produit (entrees reservoirs) vs volume consomme.

        Retourne (produit_m3, consomme_m3) sur un pas representatif."""
        wn = self.wn
        r = self.results
        flow_df = r.link["flowrate"]
        demand_df = r.node["demand"]
        dt = self._representative_dt()
        produced = 0.0
        for name in wn.link_name_list:
            sn = wn.get_link(name).start_node_name
            if sn in wn.reservoir_name_list or sn in wn.tank_name_list:
                produced += abs(float(flow_df.at[t, name]))
        consumed = sum(float(demand_df.at[t, n]) for n in wn.junction_name_list)
        return produced * dt, consumed * dt

    def _representative_dt(self) -> float:
        """Pas de temps representatif (s) entre deux releves."""
        if len(self.timestamps) < 2:
            return 3600.0
        return (self.timestamps[1] - self.timestamps[0]) or 3600.0

    def _indicative_ili(self, t) -> dict:
        """Indice Infrastructure Leakage Index (indicatif).

        UARL (l/j/km/m) est estime a partir du nombre de raccords ; faute
        de donnees d'exploitation, on derive les pertes reelles du deficit
        entre volume produit et volume consomme. Valeur a lire comme un
        ordre de grandeur, pas comme une valeur auditee."""
        produced, consumed = self._water_balance(t)
        real_losses = max(0.0, produced - consumed)
        # Estimation grossiere de la longueur de reseau (km).
        total_len_km = sum((self.wn.get_link(p).length or 0.0) for p in self.wn.pipe_name_list) / 1000.0
        uarl = 6.0  # l/j/km/m typique pour un reseau recent
        ili = real_losses / (uarl * max(1.0, total_len_km) * (24 * 3600)) if total_len_km else None
        return {"ili": round(ili, 2) if ili is not None else None,
                "real_losses_m3_s": round(real_losses, 6),
                "network_km": round(total_len_km, 2),
                "note": "Valeur indicative, non auditee."}

    # ==================================================================
    #  QUALITE DE L'EAU (Phase 5)
    #  Chlore residuel, age de l'eau, alerte sanitaire.
    # ==================================================================
    def run_quality_analysis(self) -> dict:
        """Simule la qualite (chlore residuel + age de l'eau) et produit
        une alerte sanitaire selon les seuils reglementaires.

        Le chlore residuel recommande au point de livraison est compris
        entre 0.2 et 0.5 mg/L (OMS) ; au-dela de 4 mg/L il devient
        perceptible au gout. On signale les nœuds hors plage.
        """
        self._require_loaded()
        wn = self.wn
        sources = wn.reservoir_name_list or wn.tank_name_list
        if not sources:
            return {"available": False, "note": "Aucune source pour la qualite."}

        backup_wn = copy.deepcopy(wn)
        try:
            wn2 = copy.deepcopy(wn)
            wn2.options.quality.parameter = "Chlorine"
            # Source de desinfection : chlore residuel fixe en sortie de source.
            wn2.add_source("chl_src", sources[0], "SETPOINT", 1.0)
            res = self._simulate(wn2)
        except Exception as e:
            return {"available": False, "note": f"Analyse qualite indisponible : {e}"}

        q_df = res.node.get("quality")
        if q_df is None:
            return {"available": False, "note": "Aucune donnee de qualite produite."}

        t_last = q_df.index[-1]
        junctions = wn.junction_name_list
        chlorine = {}
        alerts = []
        LOW, HIGH = 0.2, 4.0
        low_count = high_count = 0
        for n in junctions:
            try:
                val = float(q_df.at[t_last, n])
            except Exception:
                continue
            chlorine[n] = round(val, 3)
            if val < LOW:
                low_count += 1
            elif val > HIGH:
                high_count += 1
        if low_count:
            alerts.append({
                "severity": "warn",
                "title": "Desinfection insuffisante",
                "detail": f"{low_count} nœuds ont un chlore residuel < {LOW} mg/L "
                          f"(risque microbiologique)."})
        if high_count:
            alerts.append({
                "severity": "warn",
                "title": "Chlore excessif (gout/odeur)",
                "detail": f"{high_count} nœuds depassent {HIGH} mg/L."})
        if not alerts:
            alerts.append({
                "severity": "ok",
                "title": "Qualite conforme",
                "detail": "Le chlore residuel est dans les plages usuelles."})

        return {
            "available": True,
            "source": sources[0],
            "parameter": "Chlorine (mg/L)",
            "chlorine": chlorine,
            "alerts": alerts,
            "low_count": low_count,
            "high_count": high_count,
            "note": "Simulation indicative, a valider par des analyses terrain.",
        }
