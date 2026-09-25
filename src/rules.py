from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 运动员方在案件立案后可申请B样复核的保留期（天）。
REVIEW_RETENTION_DAYS = 14

# 案件结论字段只能由对应的专用动作写入，普通更新不得覆盖。
PROTECTED_CASE_FIELDS = {
    "initial_result": "provisional_suspend",
    "review_result": "record_review",
}


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")
    for field in PROTECTED_CASE_FIELDS:
        if field in data:
            raise ConflictError("%s is set by a dedicated lab action only" % field)
    filed_on = _date_ordinal(data.get("filed_at"))
    data["retention_until"] = (
        datetime.fromordinal(filed_on) + timedelta(days=REVIEW_RETENTION_DAYS)
    ).date().isoformat()


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_provisional_suspend(actor, entity, data, lookup):
    # 初检阳性即临时禁赛，初检结论随此动作固定下来。
    return {"initial_result": "positive", "suspended_by": actor.user_id}


def _validate_request_review(actor, entity, data, lookup):
    requested_on = _date_ordinal(data.get("requested_at"))
    retention_until = entity["data"].get("retention_until")
    if retention_until and requested_on > _date_ordinal(retention_until):
        raise ValidationError(
            "B-sample review request must be made within the retention period"
        )
    return {"requested_by": actor.user_id}


def _validate_record_review(actor, entity, data, lookup):
    result = data.get("review_result")
    if result not in ("positive", "not_detected"):
        raise ValidationError("review_result must be positive or not_detected")
    extra = {
        "review_result": result,
        "reviewed_by": actor.user_id,
        "lab_report_id": data["lab_report_id"],
    }
    if result == "positive":
        # 复核仍为阳性，继续临时禁赛，案件进入正常听证流程。
        target = "suspended"
    else:
        # 复核未检出：与初检阳性不一致，两份结论都留在案件里，
        # 由案件负责人重新处置，普通更新不能直接结束案件。
        target = "review_conflict"
    return target, extra


def _validate_lapse_retention(actor, entity, data, lookup):
    if entity["data"].get("review_result"):
        raise InvalidTransition("review result already recorded")
    return {"decision": "no_sanction", "lapsed_by": actor.user_id}


def _validate_redispose(actor, entity, data, lookup):
    if data.get("decision") != "no_sanction":
        raise ValidationError("redispose of an inconsistent review must be no_sanction")
    return {"redisposed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    return {"decided_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case}
CUSTOM_TRANSITIONS = {
    ('sample', 'report_adverse'): _validate_report_adverse,
    ('case', 'provisional_suspend'): _validate_provisional_suspend,
    ('case', 'request_review'): _validate_request_review,
    ('case', 'record_review'): _validate_record_review,
    ('case', 'lapse_retention'): _validate_lapse_retention,
    ('case', 'redispose'): _validate_redispose,
    ('case', 'decide'): _validate_case_decision,
    ('case', 'resolve_appeal'): _validate_case_decision,
}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open'}
    TRANSITIONS = {
        'athlete': {
            'retire': (('active',), 'retired'),
        },
        'sample': {
            'collect': (('scheduled',), 'collected'),
            'seal': (('collected',), 'sealed'),
            'ship': (('sealed',), 'in_transit'),
            'receive': (('in_transit',), 'received'),
            'analyze': (('received',), 'analyzed'),
            'report_adverse': (('analyzed',), 'adverse'),
            'clear': (('analyzed',), 'cleared'),
        },
        'case': {
            # 立案后初检阳性即临时禁赛。
            'provisional_suspend': (('open',), 'suspended'),
            # 运动员方在保留期内申请B样复核，等待实验室结论。
            'request_review': (('suspended',), 'review_pending'),
            # 实验室回传复核结论：阳性继续禁赛；未检出转由负责人重新处置。
            'record_review': (('review_pending',), None),
            # 保留期已过且没有复核结论，按无处罚结束。
            'lapse_retention': (('suspended', 'review_pending'), 'closed'),
            # 初检与复核不一致时，案件负责人重新处置，以无处罚结束。
            'redispose': (('review_conflict',), 'closed'),
            'schedule_hearing': (('suspended',), 'hearing'),
            'decide': (('hearing',), 'closed'),
            'appeal': (('closed',), 'appeal'),
            'resolve_appeal': (('appeal',), 'closed'),
        },
    }
    CREATE_REQUIRED = {
        'athlete': ('name', 'discipline'),
        'sample': ('athlete_id', 'sample_code', 'event'),
        'case': ('athlete_id', 'sample_id', 'alleged_rule', 'filed_at'),
    }
    ACTION_REQUIRED = {
        ('sample', 'collect'): ('collected_at',),
        ('sample', 'seal'): ('seal_id',),
        ('sample', 'ship'): ('carrier',),
        ('sample', 'receive'): ('lab_id',),
        ('sample', 'analyze'): ('result',),
        ('sample', 'clear'): ('reason',),
        ('case', 'provisional_suspend'): ('reason',),
        ('case', 'request_review'): ('requested_at',),
        ('case', 'record_review'): ('review_result', 'lab_report_id'),
        ('case', 'schedule_hearing'): ('hearing_at',),
        ('case', 'decide'): ('decision',),
        ('case', 'redispose'): ('decision',),
        ('case', 'appeal'): ('grounds',),
        ('case', 'resolve_appeal'): ('decision',),
    }
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel')}
    ROLE_ACTIONS = {
        'retire': ('admin', 'panel'),
        'collect': ('admin', 'inspector'),
        'seal': ('admin', 'inspector'),
        'ship': ('admin', 'inspector'),
        'receive': ('admin', 'lab'),
        'analyze': ('admin', 'lab'),
        'report_adverse': ('admin', 'lab'),
        'clear': ('admin', 'lab'),
        'provisional_suspend': ('admin', 'panel'),
        'request_review': ('admin', 'athlete', 'panel'),
        'record_review': ('admin', 'lab'),
        'lapse_retention': ('admin', 'panel'),
        'redispose': ('admin', 'panel'),
        'schedule_hearing': ('admin', 'panel'),
        'decide': ('admin', 'panel'),
        'appeal': ('admin', 'panel'),
        'resolve_appeal': ('admin', 'panel'),
    }

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
        patch = dict(data)
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        if custom:
            result = custom(actor, entity, data, lookup)
            # 自定义校验可返回 (目标状态, 额外字段) 来按数据选择后继状态。
            if isinstance(result, tuple):
                next_status, extra = result
            else:
                extra = result
            if extra:
                patch.update(extra)
        if kind == "case":
            self._protect_conclusions(entity, action, patch)
        return next_status, patch

    @staticmethod
    def _protect_conclusions(entity, action, patch):
        # 初检/复核结论只能由各自的专用动作写入，后到的普通更新不能覆盖。
        for field, owner_action in PROTECTED_CASE_FIELDS.items():
            if field in patch and action != owner_action:
                raise ConflictError("%s cannot be overwritten by %s" % (field, action))


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date().toordinal()
    except ValueError:
        raise ValidationError("invalid date: %r" % value)
