# Clipper

Transforme des vidéos YouTube longues (podcasts, interviews, formations) en clips de 2 à 5 minutes et les publie sur une chaîne YouTube de clips.

## Prérequis

- Python 3.11+
- ffmpeg (`brew install ffmpeg` sur macOS)
- Un compte [Deepgram](https://console.deepgram.com/) (transcription — ~$0,006/min)
- Un compte [Anthropic](https://console.anthropic.com/) (sélection clips + titres)
- Un projet Google Cloud avec l'API YouTube Data v3 activée (publication)

## Installation

```bash
git clone <repo>
cd Clipper

# Environnement virtuel
python -m venv .venv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate       # Windows

# Dépendances
pip install -e ".[dev]"

# Configuration
cp .env.example .env
# Éditer .env : renseigner DEEPGRAM_API_KEY et ANTHROPIC_API_KEY
```

## Utilisation rapide

```bash
# Ajouter une vidéo et lancer le pipeline
clipper add https://www.youtube.com/watch?v=XXXX

# Voir l'état
clipper status

# Interface de revue (Phase 2)
clipper review
# → ouvre http://localhost:8000

# Publier les clips validés (Phase 3)
clipper publish
```

## Configuration

Tous les réglages sont dans `config.yaml` (commenté). Les clés API sont dans `.env`.

## Configuration OAuth YouTube

> Nécessaire uniquement pour la Phase 3 (publication).

1. Aller sur [Google Cloud Console](https://console.cloud.google.com/)
2. Créer un projet → activer l'API **YouTube Data v3**
3. Créer des identifiants OAuth 2.0 (type : Application de bureau)
4. Télécharger le fichier JSON et noter `client_id` et `client_secret`
5. Renseigner dans `.env` :
   ```
   YOUTUBE_CLIENT_ID=votre_client_id
   YOUTUBE_CLIENT_SECRET=votre_client_secret
   ```
6. Lors du premier `clipper publish`, un navigateur s'ouvre pour autoriser l'accès.
   Le token est sauvegardé dans `data/youtube_token.json` pour les prochaines fois.

## Structure des données

```
data/
├── downloads/          # Vidéos sources + audio extraits
├── clips/              # Clips découpés
├── thumbnails/         # Miniatures générées
└── clipper.db          # Base SQLite (état du pipeline)
```

## Phases

| Phase | Fonctionnalités |
|-------|----------------|
| **1** ✅ | Ingestion (yt-dlp), transcription (Deepgram), sélection (Claude) |
| **2** ✅ | Bornes, découpe ffmpeg, miniatures, interface de revue |
| **3** ✅ | Publication YouTube, gestion quota (6 uploads/jour max) |
| **4** ✅ | Suivi stats J+2 / J+7, export CSV |
