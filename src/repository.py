from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS exposures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    employee_no TEXT NOT NULL,
                    dose REAL NOT NULL DEFAULT 0,
                    notify_method TEXT NOT NULL,
                    follow_up_required INTEGER NOT NULL DEFAULT 0
                        CHECK(follow_up_required IN (0,1)),
                    appointment_at TEXT,
                    notified_severity TEXT,
                    notified_at TEXT,
                    confirmed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, employee_no)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def update_item_severity(self, item_id: int, severity: str,
                             expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET severity=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (severity, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _exposure(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["follow_up_required"] = bool(result["follow_up_required"])
        return result

    def add_exposure(self, item_id: int, employee_no: str, dose: float,
                     notify_method: str, follow_up_required: bool,
                     appointment_at: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO exposures(item_id, employee_no, dose, notify_method,
                       follow_up_required, appointment_at, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item_id, employee_no, dose, notify_method,
                     1 if follow_up_required else 0, appointment_at, actor, now, now),
                )
                exposure_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"员工{employee_no}在本事件已有暴露登记记录") from exc
        return self.get_exposure(item_id, exposure_id)

    def get_exposure(self, item_id: int, exposure_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM exposures WHERE id=? AND item_id=?",
                (exposure_id, item_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("暴露人员记录不存在")
        return self._exposure(row)

    def list_exposures(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM exposures WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._exposure(row) for row in rows]

    def find_exposure(self, item_id: int, employee_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM exposures WHERE item_id=? AND employee_no=?",
                (item_id, employee_no),
            ).fetchone()
        return self._exposure(row) if row else None

    def update_exposure(self, item_id: int, exposure_id: int,
                        fields: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"dose", "notify_method", "follow_up_required", "appointment_at"}
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return self.get_exposure(item_id, exposure_id)
        assignments = ", ".join(f"{key}=?" for key in changes)
        params: list = list(changes.values())
        params.extend([utc_now(), exposure_id, item_id])
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE exposures SET {assignments}, updated_at=? WHERE id=? AND item_id=?",
                params,
            )
            if cur.rowcount == 0:
                raise NotFoundError("暴露人员记录不存在")
        return self.get_exposure(item_id, exposure_id)

    def mark_exposure_notified(self, item_id: int, exposure_id: int,
                               severity: str, notified_at: str) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE exposures SET notified_severity=?, notified_at=?,
                   confirmed_at=NULL, updated_at=? WHERE id=? AND item_id=?""",
                (severity, notified_at, notified_at, exposure_id, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("暴露人员记录不存在")

    def mark_exposure_confirmed(self, item_id: int, exposure_id: int,
                                confirmed_at: str) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE exposures SET confirmed_at=?, updated_at=? WHERE id=? AND item_id=?",
                (confirmed_at, confirmed_at, exposure_id, item_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("暴露人员记录不存在")

    def reset_exposure_notifications(self, item_id: int) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE exposures SET notified_severity=NULL, notified_at=NULL,
                   confirmed_at=NULL, updated_at=? WHERE item_id=?""",
                (utc_now(), item_id),
            )
            return cur.rowcount

    def exposure_summary(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT i.id AS item_id, i.title AS title, i.status AS status,
                   COUNT(e.id) AS exposure_count,
                   SUM(CASE WHEN e.notified_severity IS NULL
                             OR e.notified_severity <> i.severity THEN 1 ELSE 0 END)
                       AS pending_notification,
                   SUM(CASE WHEN e.follow_up_required=1 AND e.appointment_at IS NULL
                             THEN 1 ELSE 0 END) AS pending_follow_up,
                   SUM(CASE WHEN (e.follow_up_required=1 AND e.appointment_at IS NULL)
                             THEN 1 ELSE 0 END)
                       + SUM(CASE WHEN i.threshold > 0 AND e.dose >= i.threshold
                             AND e.confirmed_at IS NULL THEN 1 ELSE 0 END)
                       AS blocking_count,
                   GROUP_CONCAT(CASE WHEN e.notified_severity IS NULL
                             OR e.notified_severity <> i.severity
                             THEN e.employee_no END) AS pending_notification_employees,
                   GROUP_CONCAT(CASE WHEN e.follow_up_required=1 AND e.appointment_at IS NULL
                             THEN e.employee_no END) AS pending_follow_up_employees
            FROM items i LEFT JOIN exposures e ON e.item_id=i.id
            WHERE i.status <> 'closed'
            GROUP BY i.id ORDER BY i.id DESC
        """
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("exposure_count", "pending_notification",
                        "pending_follow_up", "blocking_count"):
                item[key] = int(item[key] or 0)
            result.append(item)
        return result

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
