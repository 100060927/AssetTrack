from .models import AuditEntry

RECORD_TYPES = {
    'Asset': 'Item', 'ResourceRequest': 'Request', 'ApprovalCase': 'Approval',
    'Transfer': 'Transfer', 'MaintenanceTask': 'Maintenance', 'EndOfLifeProposal': 'End of life',
    'ReportDraft': 'Report', 'User': 'User', 'Department': 'Department', 'Category': 'Category',
}


def _label(record):
    for attr in ('tag', 'code'):
        value = getattr(record, attr, None)
        if value:
            name = getattr(record, 'name', None) or getattr(record, 'title', None)
            return f'{value} {name}' if name else value
    return str(record)[:160]


def audit(actor, action, record=None, details='', record_type=None):
    """Write one audit line, inside the caller's transaction (all or nothing)."""
    if record is not None:
        kind = record_type or RECORD_TYPES.get(type(record).__name__, type(record).__name__)
        label = _label(record)
    else:
        kind, label = record_type or 'System', ''
    return AuditEntry.objects.create(
        actor=actor if getattr(actor, 'pk', None) else None, action=action[:60],
        record_type=kind[:40], record_label=label[:160], details=(details or '')[:500])
