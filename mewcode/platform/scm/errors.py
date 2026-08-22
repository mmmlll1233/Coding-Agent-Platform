class ScmError(RuntimeError):
    code = "SCM_ERROR"


class ScmUnavailable(ScmError):
    code = "SCM_UNAVAILABLE"


class ScmPolicyError(ScmError):
    code = "SCM_POLICY_VIOLATION"


class ScmDeliveryConflict(ScmError):
    code = "SCM_DELIVERY_CONFLICT"


class NoChangesError(ScmPolicyError):
    code = "NO_CHANGES"
