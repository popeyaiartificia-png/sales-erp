#!/usr/bin/env python3
"""Preview an ERP state import; --apply backs up SQLite and imports without overwriting cloud data."""

import argparse
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PROJECT_REF = 'sbxtmkhbtbkajvqfbbnf'
PROJECT_URL = f'https://{PROJECT_REF}.supabase.co'


def read_state(database):
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        row = source.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    if not row:
        raise ValueError('The local database has no ERP state to import.')
    state = json.loads(row[0])
    if not isinstance(state, dict) or not isinstance(state.get('leads'), list):
        raise ValueError('The local ERP state must be an object with a leads list.')
    return state


def backup_database(database):
    backup_dir = database.parent / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    target = backup_dir / f'before-supabase-{stamp}.db'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
    return target


def request_json(base_url, secret, path, body=None):
    request = urllib.request.Request(
        base_url + '/rest/v1/' + path,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
        headers={'apikey': secret, 'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST' if body is not None else 'GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Response bodies may contain submitted data; keep them out of logs.
        detail = 'Check the migration, project URL, and server-only key.'
        if error.code == 409:
            detail = 'Cloud ERP data already exists. No data was overwritten.'
        raise RuntimeError(f'Supabase returned HTTP {error.code}. {detail}') from None
    except urllib.error.URLError:
        raise RuntimeError('Could not reach Supabase. Retry the same import after checking the connection.') from None


def apply_import(database, base_url, secret):
    if base_url != PROJECT_URL:
        raise ValueError(f'This import targets the approved project {PROJECT_REF} only.')
    if not secret.startswith('sb_secret_'):
        raise ValueError('Set SUPABASE_SECRET_KEY to a server-only Supabase secret key.')
    # Check schema and read access before creating a backup or attempting a write.
    request_json(base_url, secret, 'salesquest_kv?select=k&limit=1')
    backup = backup_database(database)
    # Import the exact state in the backup so concurrent local writes cannot change the snapshot.
    snapshot = read_state(backup)
    result = request_json(base_url, secret, 'rpc/salesquest_import_state', {'p_state': snapshot})
    rows = request_json(base_url, secret, 'salesquest_kv?k=eq.state&select=v')
    if len(rows) != 1 or json.loads(rows[0]['v']) != snapshot:
        raise RuntimeError('Import verification failed. The local backup is preserved; do not overwrite cloud data.')
    return {'verified': True, 'leads': len(snapshot['leads']),
            'already_imported': bool(result.get('already_imported')), 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'salesquest_data' / 'salesquest.db')
    parser.add_argument('--apply', action='store_true', help='Back up SQLite, import state, and verify the cloud copy.')
    args = parser.parse_args()
    try:
        state = read_state(args.database)
        summary = {'mode': 'preview', 'target_project': PROJECT_REF, 'leads': len(state['leads']),
                   'databases': len(state.get('dbs', [])), 'sequences': len(state.get('sequences', [])),
                   'credentials_and_message_queues': 'excluded'}
        if args.apply:
            summary = apply_import(args.database,
                                   os.environ.get('SUPABASE_URL', PROJECT_URL).rstrip('/'),
                                   os.environ.get('SUPABASE_SECRET_KEY', ''))
        print(json.dumps(summary, indent=2))
        return 0
    except (ValueError, RuntimeError, sqlite3.Error, OSError, KeyError, TypeError) as error:
        print(f'Import stopped: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
