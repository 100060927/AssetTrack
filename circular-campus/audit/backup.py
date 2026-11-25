"""UC-30: save the whole database to one file, and put it back.

A backup is a JSON file made by Django's own `dumpdata`, so it works the same
on SQLite and PostgreSQL. Restoring first saves a safety copy of the current
data, then empties the database and loads the chosen file. If loading fails,
the safety copy is loaded back, so the system is never left half-restored.
"""
import json
import re
from collections import Counter
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from accounts.permissions import require
from core.errors import DomainError

# Rebuilt automatically by Django, or only meaningful for one browser session.
EXCLUDE = ['contenttypes', 'auth.permission', 'sessions', 'admin.logentry']
NAME = re.compile(r'^backup-[0-9]{8}-[0-9]{6}(-[a-z-]+)?\.json$')


def backup_dir():
    path = Path(settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_backups():
    files = sorted(backup_dir().glob('backup-*.json'), reverse=True)
    return [{'name': f.name, 'size_kb': round(f.stat().st_size / 1024, 1),
             'created': timezone.datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.get_current_timezone())}
            for f in files if NAME.match(f.name)]


def path_for(name):
    """Only plain backup file names inside the backup folder are accepted."""
    if not NAME.match(name or ''):
        raise DomainError('Unknown backup file.')
    path = backup_dir() / name
    if not path.exists():
        raise DomainError('That backup file no longer exists.')
    return path


def counts_in_file(path):
    with open(path, encoding='utf-8') as handle:
        return Counter(row['model'] for row in json.load(handle))


def counts_in_database(labels):
    return {label: apps.get_model(label).objects.count() for label in labels}


def _dump(suffix=''):
    stamp = timezone.localtime().strftime('%Y%m%d-%H%M%S')
    path = backup_dir() / f'backup-{stamp}{"-" + suffix if suffix else ""}.json'
    for letter in 'bcdefghijklmnopqrstuvwxyz':  # two backups in the same second
        if not path.exists():
            break
        path = path.with_name(f'{path.stem}-{letter}.json')
    call_command('dumpdata', exclude=EXCLUDE, indent=1, output=str(path), verbosity=0)
    return path


def create_backup(actor):
    require(actor, 'backup.manage')
    path = _dump()
    from .services import audit
    audit(actor, 'Backup created', None, path.name, record_type='System')
    return path, sum(counts_in_file(path).values())


def restore_backup(actor, name, confirmation):
    """Returns (safety copy path, {model: (expected, found)})."""
    require(actor, 'backup.manage')
    if confirmation != 'RESTORE':
        raise DomainError('Type RESTORE in capitals to confirm.')
    path = path_for(name)
    expected = counts_in_file(path)  # also proves the file is readable JSON
    safety = _dump('before-restore')
    actor_id, actor_name = actor.pk, actor.username
    try:
        call_command('flush', interactive=False, verbosity=0)
        call_command('loaddata', str(path), verbosity=0)
    except Exception as error:
        call_command('flush', interactive=False, verbosity=0)
        call_command('loaddata', str(safety), verbosity=0)
        raise DomainError(f'The restore failed and the previous data was put back. ({error})')
    found = counts_in_database(expected)
    check = {label: (expected[label], found[label]) for label in expected}
    from accounts.models import User
    from .services import audit
    who = User.objects.filter(pk=actor_id, username=actor_name).first()
    ok = all(a == b for a, b in check.values())
    audit(who, 'Database restored', None,
          f'From {name}. Safety copy: {safety.name}. Counts {"verified" if ok else "DIFFERENT"}.',
          record_type='System')
    return safety, check
