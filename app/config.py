from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT
DB_PATH = ROOT / "leads.db"
CITAS_PATH = ROOT / "citas.txt"
CITAS_DONE_PATH = ROOT / "citas_finalizadas.txt"
DASHBOARD_HTML = ROOT / "dashboard.html"
NM_FILE = ROOT / "nm.txt"
EMAIL_FILE = ROOT / "email.txt"
GRAPH_BASE = "https://graph.facebook.com/v21.0"
NEGOCIO = os.getenv("NEGOCIO_NOMBRE", "Parabrisas solution")
PRECIO = os.getenv("PRECIO", "210")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
STAFF_USER = os.getenv("STAFF_USER", "chat")
STAFF_PASSWORD = os.getenv("STAFF_PASSWORD", "")
