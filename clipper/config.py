"""Chargement de la configuration depuis config.yaml et les variables d'environnement."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Charge .env au premier import
load_dotenv()

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config(path: Path | None = None) -> dict:
    config_path = path or _DEFAULT_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def get_storage_path(cfg: dict) -> Path:
    raw = cfg.get("storage_path", "data/")
    p = Path(raw)
    if not p.is_absolute():
        # relatif à la racine du projet (parent du package)
        p = Path(__file__).parent.parent / p
    return p.resolve()


def get_api_key(name: str) -> str:
    """Retourne la valeur d'une clé API depuis l'environnement, lève si absente."""
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(
            f"Variable d'environnement manquante : {name}\n"
            f"Copier .env.example en .env et renseigner la valeur."
        )
    return value
