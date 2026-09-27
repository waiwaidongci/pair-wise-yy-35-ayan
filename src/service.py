from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, require_bool,
                     require_iso_datetime, require_number, require_text)
from .repository import Repository
from .rules import (APPOINTMENT_ROLES, AUDIT_ROLES, CONFIRM_ROLES,
                    CREATE_ROLES, ENTITY, EXPOSURE_CREATE_ROLES,
                    EXPOSURE_UPDATE_ROLES, NOTIFY_METHODS, NOTIFY_ROLES,
                    RECORD_ROLES, SEVERITY_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, exposure_blockers,
                    priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)

_SEVERITY_LABELS = {'low': '低', 'elevated': '升高', 'high': '高',
                    'critical': '危急'}


def _severity_label(severity: str) -> str:
    return _SEVERITY_LABELS.get(severity, severity)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target == "closed":
            blockers += exposure_blockers(item, self.repository.list_exposures(item_id))
        if blockers:
            raise ConflictError("事件尚不能关闭，存在未完成项", blockers)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def _open_item(self, item_id: int) -> Dict[str, Any]:
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能再修改暴露人员台账")
        return item

    @staticmethod
    def _notify_method(value: Any) -> str:
        value = require_text(value, "notify_method", 50)
        if value not in NOTIFY_METHODS:
            raise ValueError(f"notify_method必须是以下之一: {','.join(NOTIFY_METHODS)}")
        return value

    def add_exposure(self, item_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, EXPOSURE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self._open_item(item_id)
        employee_no = require_text(payload.get("employee_no"), "employee_no", 100)
        dose = require_number(payload.get("dose"), "dose")
        notify_method = self._notify_method(payload.get("notify_method"))
        follow_up = require_bool(payload.get("follow_up_required", False),
                                 "follow_up_required")
        appointment_at = payload.get("appointment_at")
        if appointment_at is not None:
            if not follow_up:
                raise ValidationError("只有需要随访的人员才能预约随访时间")
            appointment_at = require_iso_datetime(appointment_at, "appointment_at")
        if self.repository.find_exposure(item_id, employee_no):
            raise ConflictError(f"员工{employee_no}在本事件已有暴露登记记录")
        exposure = self.repository.add_exposure(
            item_id, employee_no, dose, notify_method, follow_up,
            appointment_at, actor)
        self.repository.append_audit("exposure_register", "暴露人员", item_id, actor, {
            "exposure_id": exposure["id"], "employee_no": employee_no,
            "dose": dose, "notify_method": notify_method,
            "follow_up_required": follow_up,
        })
        return self._decorate_exposure(item, exposure)

    def list_exposures(self, item_id: int, role: str) -> list:
        self._view(role)
        item = self.repository.get_item(item_id)
        return [self._decorate_exposure(item, exposure)
                for exposure in self.repository.list_exposures(item_id)]

    def update_exposure(self, item_id: int, exposure_id: int,
                        payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, EXPOSURE_UPDATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self._open_item(item_id)
        before = self.repository.get_exposure(item_id, exposure_id)
        # 随访标记与预约属于健康物理师决定，防止用通用更正接口绕过预约角色
        if ("appointment_at" in payload or "follow_up_required" in payload) \
                and role not in APPOINTMENT_ROLES:
            raise PermissionDenied("随访标记和预约仅health_physicist可修改")
        fields: Dict[str, Any] = {}
        changed: Dict[str, Any] = {}
        if "dose" in payload:
            dose = require_number(payload.get("dose"), "dose")
            fields["dose"] = dose; changed["dose"] = dose
        reset_notification = False
        if "notify_method" in payload:
            method = self._notify_method(payload.get("notify_method"))
            fields["notify_method"] = method; changed["notify_method"] = method
            reset_notification = method != before["notify_method"]
        if "follow_up_required" in payload:
            follow_up = require_bool(payload.get("follow_up_required"),
                                     "follow_up_required")
            fields["follow_up_required"] = 1 if follow_up else 0
            changed["follow_up_required"] = follow_up
            if not follow_up:
                fields["appointment_at"] = None
                if before.get("appointment_at"):
                    changed["appointment_cancelled"] = before["appointment_at"]
        if "appointment_at" in payload:
            follow_up = bool(fields.get("follow_up_required",
                                        before["follow_up_required"]))
            appointment_at = payload.get("appointment_at")
            if appointment_at is not None:
                if not follow_up:
                    raise ValidationError("只有需要随访的人员才能预约随访时间")
                appointment_at = require_iso_datetime(appointment_at, "appointment_at")
            fields["appointment_at"] = appointment_at
            changed["appointment_at"] = appointment_at
        if reset_notification:
            fields["notified_severity"] = None
            fields["notified_at"] = None
            fields["confirmed_at"] = None
        exposure = self.repository.update_exposure(item_id, exposure_id, fields)
        if changed or reset_notification:
            action = "exposure_update"
            detail = {"exposure_id": exposure_id,
                      "employee_no": exposure["employee_no"], "changes": changed}
            if reset_notification:
                detail["notification_voided"] = True
            self.repository.append_audit(action, "暴露人员", item_id, actor, detail)
        return self._decorate_exposure(item, exposure)

    def notify_exposure(self, item_id: int, exposure_id: int, actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, NOTIFY_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self._open_item(item_id)
        exposure = self.repository.get_exposure(item_id, exposure_id)
        if (exposure["notified_severity"] == item["severity"]
                and exposure["notified_at"]):
            raise ConflictError(
                f"员工{exposure['employee_no']}已按当前{_severity_label(item['severity'])}档通知，无需重复通知")
        notified_at = utc_now()
        self.repository.mark_exposure_notified(item_id, exposure_id,
                                               item["severity"], notified_at)
        self.repository.append_audit("exposure_notify", "暴露人员", item_id, actor, {
            "exposure_id": exposure_id,
            "employee_no": exposure["employee_no"],
            "notify_method": exposure["notify_method"],
            "severity": item["severity"], "notified_at": notified_at,
        })
        return self._decorate_exposure(item, self.repository.get_exposure(item_id, exposure_id))

    def confirm_exposure(self, item_id: int, exposure_id: int, actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        self._open_item(item_id)
        exposure = self.repository.get_exposure(item_id, exposure_id)
        if not exposure["notified_at"]:
            raise ConflictError("尚未通知本人，不能确认")
        if exposure["notified_severity"] != self.repository.get_item(item_id)["severity"]:
            raise ConflictError("通知基于旧的严重度档位，需按新档重新通知后再确认")
        if exposure["confirmed_at"]:
            raise ConflictError("该人员已确认通知")
        confirmed_at = utc_now()
        self.repository.mark_exposure_confirmed(item_id, exposure_id, confirmed_at)
        self.repository.append_audit("exposure_confirm", "暴露人员", item_id, actor, {
            "exposure_id": exposure_id,
            "employee_no": exposure["employee_no"],
            "confirmed_at": confirmed_at,
        })
        return self._decorate_exposure(
            self.repository.get_item(item_id),
            self.repository.get_exposure(item_id, exposure_id))

    def schedule_follow_up(self, item_id: int, exposure_id: int,
                           payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, APPOINTMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self._open_item(item_id)
        exposure = self.repository.get_exposure(item_id, exposure_id)
        if not exposure["follow_up_required"]:
            raise ConflictError("该人员不需要医学随访")
        appointment_at = require_iso_datetime(
            payload.get("appointment_at"), "appointment_at")
        self.repository.update_exposure(item_id, exposure_id,
                                        {"appointment_at": appointment_at})
        self.repository.append_audit("follow_up_schedule", "暴露人员", item_id, actor, {
            "exposure_id": exposure_id,
            "employee_no": exposure["employee_no"],
            "appointment_at": appointment_at,
        })
        return self._decorate_exposure(item, self.repository.get_exposure(item_id, exposure_id))

    def adjust_severity(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, SEVERITY_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self._open_item(item_id)
        severity = normalize_severity(payload.get("severity"))
        expected = payload.get("expected_version", item["version"])
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        if severity == item["severity"]:
            raise ConflictError(f"严重度已是{severity}，无需调整")
        updated = self.repository.update_item_severity(item_id, severity, expected)
        self.repository.append_audit("severity_adjust", ENTITY, item_id, actor, {
            "from": item["severity"], "to": severity,
        })
        reset_count = self.repository.reset_exposure_notifications(item_id)
        if reset_count:
            self.repository.append_audit("notify_reset", "暴露人员", item_id, actor, {
                "from_severity": item["severity"], "to_severity": severity,
                "affected": reset_count,
            })
        return self.enrich(updated)

    def exposure_summary(self, role: str) -> list:
        self._view(role)
        return self.repository.exposure_summary()

    @staticmethod
    def _decorate_exposure(item: Dict[str, Any], exposure: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(exposure)
        notified_current = (
            bool(exposure["notified_at"])
            and exposure["notified_severity"] == item["severity"])
        result["notification_current"] = notified_current
        result["confirmed"] = bool(exposure["confirmed_at"])
        result["pending_notification"] = not notified_current
        result["pending_confirmation"] = bool(
            item["threshold"] and item["threshold"] > 0
            and exposure["dose"] >= item["threshold"]
            and not exposure["confirmed_at"])
        result["pending_follow_up"] = bool(
            exposure["follow_up_required"] and not exposure["appointment_at"])
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
