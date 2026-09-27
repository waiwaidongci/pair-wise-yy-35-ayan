from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_bool, require_number,
                     require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EXPOSURE_CONFIRM_ROLES,
                    EXPOSURE_FOLLOWUP_ROLES, EXPOSURE_NOTIFY_ROLES,
                    EXPOSURE_REGISTER_ROLES, RECORD_ROLES, SEVERITY_ADJUST_ROLES,
                    TERMINAL_STATES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, exposure_blockers, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


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
        blockers += exposure_blockers(
            target, self.repository.list_exposures(item_id), item["threshold"])
        if blockers:
            raise ConflictError("；".join(blockers), blockers)
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def register_exposure(self, item_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, EXPOSURE_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        employee_id = require_text(payload.get("employee_id"), "employee_id", 50)
        dose = require_number(payload.get("dose"), "dose")
        method = require_text(payload.get("notification_method"), "notification_method", 50)
        follow_up_required = require_bool(
            payload.get("follow_up_required", False), "follow_up_required")
        self.repository.get_item(item_id)
        exposure = self.repository.add_exposure(
            item_id, employee_id, dose, method, follow_up_required, actor)
        self.repository.append_audit("exposure_register", ENTITY, item_id, actor, {
            "employee_id": employee_id, "dose": dose,
            "notification_method": method, "follow_up_required": follow_up_required,
        })
        return exposure

    def list_exposures(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_exposures(item_id)

    def notify_exposure(self, item_id: int, employee_id: str, actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, EXPOSURE_NOTIFY_ROLES)
        actor = require_text(actor, "actor", 100)
        employee_id = require_text(employee_id, "employee_id", 50)
        exposure = self.repository.get_exposure(item_id, employee_id)
        if exposure["notified"]:
            raise ConflictError("该员工已通知，无需重复通知")
        updated = self.repository.mark_exposure_notified(item_id, employee_id)
        self.repository.append_audit("exposure_notify", ENTITY, item_id, actor, {
            "employee_id": employee_id,
            "notification_method": updated["notification_method"],
        })
        return updated

    def confirm_exposure(self, item_id: int, employee_id: str, actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, EXPOSURE_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        employee_id = require_text(employee_id, "employee_id", 50)
        exposure = self.repository.get_exposure(item_id, employee_id)
        if exposure["confirmed"]:
            raise ConflictError("该员工已确认，无需重复确认")
        updated = self.repository.mark_exposure_confirmed(item_id, employee_id)
        self.repository.append_audit("exposure_confirm", ENTITY, item_id, actor, {
            "employee_id": employee_id, "dose": updated["dose"],
        })
        return updated

    def schedule_follow_up(self, item_id: int, employee_id: str,
                           payload: Dict[str, Any], actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, EXPOSURE_FOLLOWUP_ROLES)
        actor = require_text(actor, "actor", 100)
        employee_id = require_text(employee_id, "employee_id", 50)
        appointment = require_timestamp(payload.get("appointment"), "appointment")
        self.repository.get_exposure(item_id, employee_id)
        updated = self.repository.set_follow_up_appointment(
            item_id, employee_id, appointment)
        self.repository.append_audit("exposure_follow_up", ENTITY, item_id, actor, {
            "employee_id": employee_id, "appointment": appointment,
        })
        return updated

    def adjust_severity(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, SEVERITY_ADJUST_ROLES)
        actor = require_text(actor, "actor", 100)
        severity = normalize_severity(payload.get("severity"))
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] in TERMINAL_STATES:
            raise ConflictError("事件已关闭，不能调整严重度")
        if severity == item["severity"]:
            raise ValidationError("新严重度与当前相同")
        updated = self.repository.update_item_severity(item_id, severity, expected_version)
        reset = self.repository.reset_exposure_notifications(item_id)
        self.repository.append_audit("severity_adjust", ENTITY, item_id, actor, {
            "from": item["severity"], "to": severity,
            "confirmations_voided": reset["confirmations_voided"],
            "notifications_reset": reset["notifications_reset"],
            "priority": priority_score(severity, item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def exposure_summary(self, role: str) -> Dict[str, int]:
        self._view(role)
        return self.repository.exposure_summary()

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
