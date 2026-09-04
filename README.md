# 🌊 Aqua Twin — Jumeau numérique hydraulique

> **Plateforme web de simulation, de contrôle et d'analyse de réseaux de distribution d'eau potable**, basée sur le moteur **WNTR / EPANET** de l'EPA américaine.

Aqua Twin transforme un fichier EPANET (`.inp`) en un **jumeau numérique interactif** : visualisation du réseau, relecture temporelle de la simulation, contrôle des pompes/vannes/conduites, **détection de fuites**, **optimisation de l'exploitation** et **analyse de la qualité de l'eau** — le tout depuis un navigateur, déployable en un clic.

---

## ✨ Fonctionnalités

| Module | Description |
|---|---|
| 🗺️ **Visualisation interactive** | Carte SVG du réseau (zoom, déplacement), flèches de débit, coloration par pression |
| 🌍 **Carte réelle (OSM)** | Superposition du réseau sur OpenStreetMap via Leaflet (coordonnées géographiques) |
| ⏱️ **Simulation étendue** | Relecture complète de la période simulée (curseur temporel, lecture auto) |
| 🕹️ **Contrôle des éléments** | Ouvrir / fermer pompes, vannes, conduites avec re-calcul hydraulique |
| 📈 **Graphes d'évolution** | Pression, débit, vitesse et niveau au fil du temps (Chart.js) |
| 🔎 **Détection de fuites** | Génération de scénarios, résidus de pression, corrélation de signatures, heatmap d'alarme |
| ⚙️ **Optimisation** | Gestion de pression / PRV, énergie de pompage, zones DMA, rendement NRW & ILI, conformité de vitesse |
| 💧 **Qualité de l'eau** | Simulation du chlore résiduel et alerte sanitaire (seuils OMS) |
| 📥 **Export CSV** | Téléchargement des données simulées en fichier `.csv` |
| 📄 **Rapport PDF** | Génération d'un rapport de simulation imprimable |
| 👥 **Multi-utilisateurs** | Sessions isolées : chaque utilisateur possède son propre moteur WNTR |
| 🌓 **Thème clair / sombre** | Interface "cockpit" moderne, thème adaptatif |

---

## 🏗️ Architecture

```
┌───────────────────────────┐         ┌────────────────────────────┐
│   Navigateur (SPA)        │  HTTP   │   Backend FastAPI          │
│   index.html (SVG/JS)     │────────▶│   api.py                   │
│   Chart.js · Leaflet      │  JSON   │      │                    │
└───────────────────────────┘         │      ▼                    │
                                      │   twin_engine.py           │
                                      │   DigitalTwin (par session)│
                                      │      │                    │
                                      │      ▼                    │
                                      │   WNTR / EPANET           │
                                      └────────────────────────────┘
```

- **Frontend** : application monopage (SPA) en **HTML / CSS / JavaScript vanilla** contenue dans un seul fichier `index.html`, avec routage par hash (`#/home`, `#/sim`, `#/charts`, `#/detect`, `#/opt`, `#/quality`, …).
- **Backend** : **FastAPI** expose une API REST. Chaque session (`X-Session-ID`) possède **son propre moteur** `DigitalTwin` (isolation complète multi-utilisateurs), avec nettoyage automatique des sessions inactives (TTL 30 min).
- **Moteur** : **WNTR 1.5 / EPANET** pour le calcul hydraulique (débits, pressions, vitesses) et la qualité de l'eau.

### Pile technologique

| Couche | Technologie |
|---|---|
| Backend | Python · FastAPI · Uvicorn |
| Moteur hydraulique | WNTR 1.5 · EPANET |
| Calcul | NumPy · Pandas |
| Frontend | HTML5 · CSS3 · JavaScript vanilla |
| Graphiques | Chart.js 4 |
| Carte réelle | Leaflet + OpenStreetMap |
| Conteneurisation | Docker |
| Hébergement | Render |

---

## 🚀 Démarrage rapide

### Prérequis

- **Python 3.11+**
- (Recommandé) un environnement virtuel

### Installation locale

```bash
# 1. Cloner / se placer dans le projet
cd Aqua_twin

# 2. Créer un environnement virtuel (optionnel mais recommandé)
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Lancer le serveur
uvicorn api:app --reload --port 8000
```

Le serveur est alors accessible sur **http://localhost:8000** : il sert directement l'interface (`index.html`) et l'API.

> ℹ️ Un **fichier de démonstration** `.inp` est recommandé. Vous pouvez en télécharger un sur le dépôt officiel de WNTR (par ex. *Net3*) ou utiliser le vôtre. Les fichiers `.inp` sont ignorés par Git (`*.inp` dans `.gitignore`).

---

## 🐳 Déploiement avec Docker

### Image locale

```bash
docker build -t aqua-twin .
docker run -p 8000:8000 aqua-twin
```

Le `Dockerfile` :
- part de `python:3.11-slim` (avec `build-essential` pour compiler les paquets scientifiques) ;
- installe les dépendances via `pip` ;
- lance `uvicorn api:app` sur le port `$PORT` (défaut **8000**).

### Déploiement sur Render

1. Créez un **Web Service** pointant vers ce dépôt.
2. **Environment** : `Docker` (le `Dockerfile` est détecté automatiquement).
3. Render injecte la variable `PORT` ; le serveur s'y adapte automatiquement (le frontend détecte lui-même l'URL de l'API, y compris derrière le reverse-proxy Render).

---

## 📚 Référence de l'API

Toutes les requêtes, sauf l'upload, s'échangent du JSON. Le header **`X-Session-ID`** identifie la session (par défaut `default`) ; chaque session possède un moteur indépendant.

| Méthode | Route | Description |
|---|---|---|
| `GET` | `/` | Sert l'interface (`index.html`) |
| `GET` | `/api/health` | État du service et du réseau chargé |
| `POST` | `/api/upload` | Charge un fichier `.inp` (multipart) |
| `GET` | `/api/topology` | Nœuds et liens (topologie du réseau) |
| `GET` | `/api/things` | Inventaire des éléments (pompes, vannes, conduites…) |
| `GET` | `/api/snapshot` | État hydraulique courant (pression, débit, vitesse) |
| `POST` | `/api/advance` | Avance d'un pas de temps |
| `POST` | `/api/tick` | Va à un pas de temps donné |
| `POST` | `/api/reset-time` | Revient au début de la simulation |
| `POST` | `/api/reset-controls` | Recharge le réseau source (annule contrôles, fuites, scénarios) |
| `PUT` | `/api/control` | Ouvre / ferme un lien (`open` / `closed`) |
| `GET` | `/api/history/{thing_id}` | Historique temporel complet d'un élément |
| `GET` | `/api/network-history` | Indicateurs globaux du réseau au fil du temps |
| `GET` | `/api/settings` | Paramètres serveur (version, sessions actives, TTL) |
| `POST` | `/api/leak` | Applique une fuite (casse) sur une conduite |
| `POST` | `/api/clear-leaks` | Efface fuites et scénarios |
| `GET` | `/api/scenarios` | Liste des scénarios métier disponibles |
| `POST` | `/api/scenario` | Applique un scénario métier |
| `POST` | `/api/pressure-driven` | Active / désactive la demande dépendante de la pression (PDA) |
| `POST` | `/api/detect-leaks` | Lance la détection / localisation de fuite |
| `GET` | `/api/optimization` | Indicateurs d'optimisation (pression, énergie, zones, ILI, vitesse) |
| `POST` | `/api/quality` | Simule la qualité de l'eau et produit l'alerte sanitaire |

La documentation interactive (Swagger UI) est disponible sur `/docs`.

---

## 📁 Structure du projet

```
Aqua_twin/
├── api.py            # API REST FastAPI + gestion des sessions
├── twin_engine.py    # Cœur du moteur : DigitalTwin (simulation, contrôle,
│                     #   fuites, scénarios, détection, optimisation, qualité)
├── index.html        # Frontend complet (SPA : pages, cartes, graphes, rapports)
├── requirements.txt  # Dépendances Python (versions testées)
├── Dockerfile        # Image Docker (déploiement Render via détection Dockerfile)
└── README.md         # Ce document
```

---

## 🧠 Modules d'analyse

### 🔎 Détection de fuites
`twin_engine.run_leak_detection()` localise une fuite par **analyse des résidus de pression** :

1. Simulation de référence **sans fuite** → pressions nominales ;
2. **Capteurs** : sous-échantillon réaliste de jonctions (télémesure limitée) ;
3. Pour chaque conduite candidate, simulation d'une fuite unitaire → **signature** (variation de pression aux capteurs) ;
4. **Corrélation de Pearson** entre chaque signature et l'écart observé → **classement** des conduites suspectes ;
5. **Heatmap d'alarme** des chutes de pression (zone de fuite probable).

Le nombre de candidats est borné pour rester interactif ; en production, cette analyse est précalculée hors-ligne.

### ⚙️ Optimisation
`twin_engine.get_optimization()` produit des indicateurs d'exploitation :

- **Pression** : min / moyenne / max, nœuds en déficit (< 20 m), **recommandation PRV** (réduction de pression, économie de pertes ∝ p^1.5) ;
- **Énergie de pompage** : P = ρ·g·Q·H / η intégrée sur la durée, coût estimé (0,12 €/kWh) ;
- **Zones de desserte (DMA simplifié)** : partition des nœuds par source (BFS) ;
- **Rendement** : taux d'**eau non facturée (NRW/NRF)** et **ILI** (Infrastructure Leakage Index, indicatif) ;
- **Conformité de la vitesse** dans la plage réglementaire **[0.5 – 1.5] m/s** :
  - < 0.5 m/s → risque de **dépôt / stagnation** ;
  - 0.5 – 1.5 m/s → **fonctionnement optimal** ;
  - \> 1.5 m/s → risque d'**érosion / bruit**.

### 💧 Qualité de l'eau
`twin_engine.run_quality_analysis()` simule la propagation du **chlore résiduel** (source de désinfection en sortie de réservoir) et signale les nœuds hors des plages usuelles (OMS : 0,2 – 4 mg/L) :

- < 0,2 mg/L → désinfection **insuffisante** (risque microbiologique) ;
- \> 4 mg/L → chlore **excessif** (gout / odeur).

---

## ⚠️ Notes méthodologiques

Les indicateurs **ILI**, **NRW** et **coûts d'énergie** sont des **estimations indicatives** calculées à partir des données simulées — ils ne se substituent pas à un audit d'exploitation (comptages terrain, sectorisation réelle, données de facturation).

Le **mode pression-dépendant (PDA)** est désactivé par défaut (comportement EPANET en demande constante). Il peut être activé via l'API `/api/pressure-driven` pour un comportement plus réaliste en période de fuite ou de pénurie.

---

## 🗺️ Feuille de route

- [ ] Apprentissage d'un **modèle ML** (forêt aléatoire / gradient boosting) pour affiner la localisation des fuites à partir d'un historique de signatures ;
- [ ] Sectorisation **DMA réelle** à partir de données d'exploitation ;
- [ ] Optimisation **multi-objectifs** (pression, énergie, coût de renouvellement) ;
- [ ] Alertes push et historique d'événements (journal des actions) ;
- [ ] Authentification et gestion des rôles.

---

## 📄 Licence

Distribué sous licence **MIT**. Projet pédagogique et de démonstration — **AS-z1**.

---

<div align="center">

*Aqua Twin v2.0 — Jumeau numérique hydraulique · WNTR / EPANET · FastAPI · Déployable sur Render*

</div>
