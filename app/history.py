"""Historique des BOM analysées par un utilisateur connecté.

On conserve le fichier d'origine, les réglages (colonnes, remplacements) et un résumé des verdicts.
Rouvrir une BOM relance l'analyse avec la base actuelle : les statuts peuvent avoir évolué.
Seules les MAX_PER_USER analyses les plus récentes sont gardées.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

MAX_PER_USER = 20


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _summary(report: dict) -> str:
    s = report["summary"]
    return json.dumps({
        "overall": s["overall"],
        "rohs2": s["rohs2"]["verdict"],
        "rohs3": s["rohs3"]["verdict"],
        "reach": s["reach"]["verdict"],
        "lines": s["lines"],
        "dnp": s["dnp"],
        "found": s["found"],
        "not_found": s["not_found"],
        "substituted": s.get("substituted", 0),
        "eccn_controlled": s.get("export", {}).get("eccn_controlled", 0),
    })


def save(conn: sqlite3.Connection, user_id: int, filename: str, data: bytes, mapping: str, substitutions: str,
         report: dict, history_id: int | None = None) -> int:
    """Enregistre une analyse, ou met à jour l'entrée history_id (même BOM, réglages modifiés)."""
    now = _now()
    if history_id:
        cur = conn.execute(
            "UPDATE bom_history SET mapping = ?, substitutions = ?, summary = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (mapping or "", substitutions or "", _summary(report), now, history_id, user_id),
        )
        if cur.rowcount:
            return history_id
    cur = conn.execute(
        "INSERT INTO bom_history (user_id, filename, file_data, mapping, substitutions, summary, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, filename, data, mapping or "", substitutions or "", _summary(report), now, now),
    )
    # On ne garde que les analyses les plus récentes
    conn.execute(
        "DELETE FROM bom_history WHERE user_id = ? AND id NOT IN "
        "(SELECT id FROM bom_history WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?)",
        (user_id, user_id, MAX_PER_USER),
    )
    return cur.lastrowid


def list_for(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, mapping, substitutions, summary, created_at, updated_at, length(file_data) AS size "
        "FROM bom_history WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
        (user_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "filename": r["filename"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "size": r["size"],
            "mapping": json.loads(r["mapping"]) if r["mapping"] else {},
            "substitutions": json.loads(r["substitutions"]) if r["substitutions"] else {},
            "summary": json.loads(r["summary"]) if r["summary"] else {},
        })
    return out


def get_file(conn: sqlite3.Connection, user_id: int, history_id: int) -> tuple[str, bytes] | None:
    row = conn.execute("SELECT filename, file_data FROM bom_history WHERE id = ? AND user_id = ?", (history_id, user_id)).fetchone()
    return (row["filename"], bytes(row["file_data"])) if row else None


def delete(conn: sqlite3.Connection, user_id: int, history_id: int) -> bool:
    return conn.execute("DELETE FROM bom_history WHERE id = ? AND user_id = ?", (history_id, user_id)).rowcount > 0
