"""Distribution session state machine: prepared -> distributing -> committed/failed."""

from .errors import E_STATE

INIT = "init"
PREPARED = "prepared"
DISTRIBUTING = "distributing"
COMMITTED = "committed"
FAILED = "failed"

ALLOWED = {
    (INIT, PREPARED),
    (PREPARED, DISTRIBUTING),
    (DISTRIBUTING, COMMITTED),
    (DISTRIBUTING, FAILED),
    (PREPARED, FAILED),
}


class StateMachine:
    """Transition log is persisted; each entry records the trigger version,
    the acknowledged node set and the timestamp (handled by the store)."""

    def __init__(self, store):
        self.store = store
        transitions = store.transitions()
        self.state = transitions[-1]["to"] if transitions else INIT

    def transition(self, dst, version, acked, reason=""):
        from .errors import PlatformError

        if (self.state, dst) not in ALLOWED:
            raise PlatformError(
                E_STATE,
                "illegal transition %s -> %s" % (self.state, dst),
                details={"from": self.state, "to": dst, "version": version},
            )
        self.store.add_transition(self.state, dst, version, acked, reason)
        self.state = dst

    def commit(self, version, acked, converged, blockers):
        """Idempotent commit. Returns a result dict; never raises for a
        repeated commit of the same version."""
        committed = self.store.committed_version()
        if committed == version and self.state == COMMITTED:
            return {"ok": True, "idempotent": True, "version": version,
                    "reason": "version %d already committed" % version}
        if committed > version:
            return {"ok": False, "code": E_STATE, "version": version,
                    "reason": "committed version %d would regress to %d"
                              % (committed, version)}
        if self.state != DISTRIBUTING or not converged:
            reason = "; ".join(blockers) if blockers else "session is not distributing"
            return {"ok": False, "code": E_STATE, "version": version,
                    "reason": "commit blocked: " + reason}
        self.transition(COMMITTED, version, acked)
        self.store.set_committed_version(version)
        return {"ok": True, "idempotent": False, "version": version}
