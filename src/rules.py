from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


REVIEW_RETENTION_DAYS = 30


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")
    # 立案时把初检结论快照进案件，之后任何普通更新都不能覆盖
    data["initial_result"] = sample["data"].get("result") or "adverse"
    deadline = data.get("review_deadline")
    if deadline:
        try:
            _date_ordinal(deadline)
        except (TypeError, ValueError):
            raise ValidationError("review_deadline must be an ISO date")
    else:
        retention_end = datetime.now(timezone.utc).date() + timedelta(days=REVIEW_RETENTION_DAYS)
        data["review_deadline"] = retention_end.isoformat()


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


def _validate_request_review(actor, entity, data, lookup):
    if entity["data"].get("review_result"):
        raise ValidationError("review already reported; cannot request again")
    deadline = entity["data"].get("review_deadline")
    if deadline and _today_ordinal() > _date_ordinal(deadline):
        raise ValidationError("retention period has passed; review can no longer be requested")
    return {"review_requested_by": actor.user_id, "review_requested_at": _now_iso()}


def _validate_report_review(actor, entity, data, lookup):
    result = data.get("review_result")
    if result not in ("confirmed", "negative"):
        raise ValidationError("review_result must be confirmed or negative")
    extra = {
        "review_reported_by": actor.user_id,
        "review_reported_at": _now_iso(),
        "review_consistent": result == "confirmed",
    }
    if result == "negative":
        # 与初检阳性不一致：两份结论都留在案件里，交案件负责人重新处置
        extra["_next_status"] = "review_disputed"
    return extra


def _validate_resolve_review(actor, entity, data, lookup):
    if data.get("decision") != "no_sanction":
        raise ValidationError("inconsistent review must be resolved with no_sanction")
    return {"decided_by": actor.user_id}


def _validate_expire_review(actor, entity, data, lookup):
    if entity["data"].get("review_result") == "confirmed":
        raise ValidationError("confirmed review cannot expire; continue the case")
    deadline = entity["data"].get("review_deadline")
    if deadline and _today_ordinal() <= _date_ordinal(deadline):
        raise ValidationError("retention period has not expired")
    return {"decision": "no_sanction", "closure": "retention_expired", "decided_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case}
CUSTOM_TRANSITIONS = {('sample', 'report_adverse'): _validate_report_adverse, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('case', 'request_review'): _validate_request_review, ('case', 'report_review'): _validate_report_review, ('case', 'resolve_review'): _validate_resolve_review, ('case', 'expire_review'): _validate_expire_review}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received',), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'request_review': (('open', 'suspended'), 'review_pending'), 'report_review': (('review_pending',), 'suspended'), 'resolve_review': (('review_disputed',), 'closed'), 'expire_review': (('open', 'suspended', 'review_pending'), 'closed'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'sample_id', 'alleged_rule')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'clear'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'report_review'): ('review_result',), ('case', 'resolve_review'): ('decision',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',)}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'clear': ('admin', 'lab'), 'provisional_suspend': ('admin', 'panel'), 'request_review': ('admin', 'panel', 'athlete'), 'report_review': ('admin', 'lab'), 'resolve_review': ('admin', 'panel'), 'expire_review': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel')}
    CONCLUSIVE_FIELDS = {'sample': ('result',), 'case': ('initial_result', 'review_result')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        # 自定义校验器可按结论内容改判去向（如复核未检出转入复核争议）
        override = patch.pop("_next_status", None)
        if override:
            next_status = override
        self._ensure_conclusions_kept(kind, entity, patch)
        return next_status, patch

    def _ensure_conclusions_kept(self, kind, entity, patch):
        # 已记录的检测结论只能保留，后到的普通更新不能覆盖
        for field in self.CONCLUSIVE_FIELDS.get(kind, ()):
            if field not in patch:
                continue
            existing = entity["data"].get(field)
            if existing not in (None, "") and patch[field] != existing:
                raise ConflictError(
                    "conclusion %s is already recorded and cannot be overwritten" % field
                )


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _today_ordinal():
    return datetime.now(timezone.utc).date().toordinal()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
