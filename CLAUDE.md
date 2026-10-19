# Clipper — CLAUDE.md

## But
Outil CLI + UI web locale pour transformer des vidéos YouTube longues (podcasts, interviews, formations) en clips de 2-5 minutes et les publier sur une chaîne YouTube de clips.

## Stack
- **Python 3.11+**, SQLite (état), FastAPI + HTML/Tailwind CDN (revue)
- **yt-dlp** : téléchargement vidéo/audio
- **Deepgram** (nova-2) : transcription horodatée au mot + diarisation
- **Anthropic Claude** (claude-sonnet-4-6 par défaut) : sélection clips + titres/tags
- **ffmpeg** : découpe précise + normalisation audio −14 LUFS
- **Pillow + OpenCV** : miniatures
- **YouTube Data v3 OAuth** : upload + programmation

## Commandes
```bash
# Installation
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # puis remplir les clés

# Pipeline complet
clipper add <URL>        # ajoute une vidéo/playlist/chaîne et lance le pipeline
clipper run              # (re)lance le pipeline pour les vidéos en attente
clipper review           # ouvre l'UI de revue sur http://localhost:8000
clipper publish          # publie les clips validés dans la file
clipper status           # état du pipeline + coûts estimés et réels
clipper stats            # télécharge les stats YouTube (J+2, J+7) + export CSV

# Tests
pytest
```

## Structure
```
clipper/        # package Python — un fichier par responsabilité
prompts/        # prompts Claude modifiables sans toucher au code
static/         # UI de revue (HTML + Tailwind CDN + vanilla JS)
data/           # gitignored — downloads/, clips/, thumbnails/, clipper.db
tests/          # pytest
config.yaml     # réglages commentés
.env            # clés API (gitignored)
```

## Conventions
- Logs en français, niveau INFO par défaut (`logging.getLogger(__name__)`)
- Commentaires en français dans le code
- Chaque commande CLI est **idempotente** : relancer ne retraite pas ce qui est déjà fait
- Une vidéo en erreur ne bloque pas les autres
- Prompts dans `prompts/*.md` — variables `{entre_accolades}` remplacées au runtime
- Clés API dans `.env`, réglages dans `config.yaml`
- Pas de dépendance inutile — chaque dépendance a une raison dans ce fichier

## Phases
| Phase | Contenu | Statut |
|-------|---------|--------|
| 1 | Ingestion, transcription, sélection | ✅ Done |
| 2 | Bornes, découpe, miniatures, revue UI | ✅ Done |
| 3 | Publication YouTube + quota | ✅ Done |
| 4 | Suivi stats (J+2, J+7) | ✅ Done |

## Coûts estimés par heure de vidéo
- Deepgram nova-2 : ~$0.35/h
- Claude sonnet-4-6 : ~$0.10-0.30/h selon longueur
- Total Phase 1 : ~$0.50/h de vidéo source
