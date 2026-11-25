"""Server-side permission rules (C1, AUTH, SW-7).

Every view and every service call goes through `require()`. Templates also ask
`user.can()` to decide what to show, but hiding a button is never the check.
"""
from django.core.exceptions import PermissionDenied

R = None  # set lazily to avoid a circular import


def _roles():
    global R
    if R is None:
        from .models import User
        R = User.Role
    return R


def _same_department(user, asset):
    return user.department_id is not None and asset.department_id == user.department_id


def _is(user, *roles):
    return user.role in roles


def _rules():
    r = _roles()
    staff = (r.REQUESTER, r.CUSTODIAN, r.APPROVER)
    return {
        # Catalog (BR-10: a custodian manages only their own department's assets)
        'asset.register': lambda u, _: _is(u, r.CUSTODIAN) and u.department_id is not None,
        'asset.manage': lambda u, a: _is(u, r.CUSTODIAN) and _same_department(u, a),
        'asset.view_inventory': lambda u, _: _is(u, r.CUSTODIAN),
        # Everyone who can hold items sees the ones they personally hold
        # Approvers and the admin see every department's assets with their details
        'asset.view_all': lambda u, _: _is(u, r.APPROVER, r.ADMIN),
        'asset.view_mine': lambda u, _: not _is(u, r.ADMIN),
        # The person holding an item can say they no longer need it
        'asset.offer_back': lambda u, a: a.owner_id == u.id and not _is(u, r.CUSTODIAN),
        # BR-11: financial values only for authorized roles
        'asset.view_cost': lambda u, _: _is(u, r.CUSTODIAN, r.APPROVER, r.ADMIN),
        'repair.view_cost': lambda u, _: _is(u, r.CUSTODIAN, r.APPROVER, r.ADMIN, r.MAINTENANCE),
        # Requests
        'request.create': lambda u, _: _is(u, *staff) and u.department_id is not None,
        'request.view': lambda u, q: (
            q.requester_id == u.id
            or _is(u, r.APPROVER, r.ADMIN)
            or (_is(u, r.CUSTODIAN) and q.reservations.filter(
                asset__department_id=u.department_id).exists())
        ),
        'request.own': lambda u, q: q.requester_id == u.id,
        # Approvals (BR-3: never your own request; BR-16: admin has no approval right)
        'approval.queue': lambda u, _: _is(u, r.APPROVER),
        'approval.decide': lambda u, case: (
            _is(u, r.APPROVER) and case.reservation.request.requester_id != u.id
        ),
        # Transfers
        'transfer.queue': lambda u, _: _is(u, r.CUSTODIAN),
        'transfer.dispatch': lambda u, t: (
            _is(u, r.CUSTODIAN)
            and t.from_department_id == u.department_id
            and t.reservation.request.requester_id != u.id
        ),
        'transfer.receive': lambda u, t: t.reservation.request.requester_id == u.id,
        'assistant.use': lambda u, _: True,
        # Open needs nobody has filled yet (UC-05 exception, UC-12 unmet demand)
        'needs.view': lambda u, _: _is(u, r.CUSTODIAN, r.APPROVER, r.ADMIN),
        # Reports
        'report.view': lambda u, _: _is(u, r.APPROVER, r.ADMIN),
        # Who took which assets (custodians see their own department's outgoing items)
        'movements.view': lambda u, _: _is(u, r.CUSTODIAN, r.APPROVER, r.ADMIN),
        'movements.all_departments': lambda u, _: _is(u, r.APPROVER, r.ADMIN),
        'report.draft': lambda u, _: _is(u, r.APPROVER, r.ADMIN),
        'report.publish': lambda u, _: _is(u, r.ADMIN),
        # UC-15: edit an asset record (own department, as for publishing)
        'asset.edit': lambda u, a: _is(u, r.CUSTODIAN) and _same_department(u, a),
        # UC-21: anyone who can see an item can report a defect on it
        'defect.report': lambda u, _: True,
        # UC-09: maintenance staff record inspections and repairs (BR-9: no disposal)
        'maintenance.work': lambda u, _: _is(u, r.MAINTENANCE),
        'maintenance.view': lambda u, _: True,  # requesters see only the problems they reported
        # UC-18: officers extend holds and run the expiry check
        'hold.extend': lambda u, _: _is(u, r.APPROVER),
        'expiry.run': lambda u, _: _is(u, r.APPROVER, r.ADMIN),
        # UC-19: the receiver reports a problem; an independent officer resolves it
        'transfer.dispute': lambda u, t: t.reservation.request.requester_id == u.id,
        'dispute.resolve': lambda u, t: (
            _is(u, r.APPROVER) and t.reservation.request.requester_id != u.id),
        # UC-20: end of life (BR-5: independent officer approval; BR-9: never maintenance)
        'eol.view': lambda u, _: _is(u, r.CUSTODIAN, r.APPROVER, r.ADMIN),
        'eol.propose': lambda u, a: (
            _is(u, r.APPROVER) or (_is(u, r.CUSTODIAN) and _same_department(u, a))),
        'eol.decide': lambda u, p: _is(u, r.APPROVER) and p.proposed_by_id != u.id,
        'eol.complete': lambda u, p: (
            _is(u, r.APPROVER) or (_is(u, r.CUSTODIAN) and _same_department(u, p.asset))),
        # UC-26 and UC-30
        'audit.view': lambda u, _: _is(u, r.APPROVER, r.ADMIN),
        'backup.manage': lambda u, _: _is(u, r.ADMIN),
    }


def can(user, action, record=None):
    if not user.is_authenticated or not user.is_active:
        return False
    rule = _rules().get(action)
    if rule is None:
        return False  # unknown actions are denied
    return bool(rule(user, record))


def require(user, action, record=None):
    if not can(user, action, record):
        raise PermissionDenied(f'Not allowed: {action}')
