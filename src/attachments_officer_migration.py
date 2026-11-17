"""Schema 7 -> 8: original Motet storage, with unchanged retained identities."""
from attachments_types import fail

FILE_FIELDS = '''id draft_id name media bytes digest generation state identity error member
 expires_at expiry_generation needs_cleanup cleanup_reserve access_generation deletion_generation preparation_kind'''.split()
MEMBER_FIELDS = '''message_id ordinal source_id source_generation access_generation identity digest
 media bytes name preparation_kind'''.split()


def files_sql(name, *, direct):
    kinds = "'text','original_relay','officer_direct'" if direct else "'text','original_relay'"
    return f'''CREATE TABLE {name} (id TEXT PRIMARY KEY,draft_id TEXT NOT NULL REFERENCES drafts(id),
      name TEXT NOT NULL,media TEXT NOT NULL,bytes INTEGER NOT NULL CHECK(bytes>=0),digest TEXT NOT NULL,
      generation INTEGER NOT NULL,state TEXT NOT NULL,identity TEXT,error TEXT,
      member INTEGER NOT NULL CHECK(member IN (0,1)),expires_at REAL DEFAULT NULL,
      expiry_generation INTEGER NOT NULL DEFAULT 0,
      needs_cleanup INTEGER NOT NULL DEFAULT 0 CHECK(needs_cleanup IN (0,1)),cleanup_reserve TEXT NOT NULL,
      access_generation INTEGER NOT NULL DEFAULT 1,deletion_generation INTEGER NOT NULL DEFAULT 0,
      preparation_kind TEXT NOT NULL DEFAULT 'text' CHECK(preparation_kind IN ({kinds})))'''


def members_sql(name, *, direct):
    check = "preparation_kind IN ('text','officer_direct')" if direct else "preparation_kind='text'"
    return f'''CREATE TABLE {name} (
      message_id TEXT NOT NULL REFERENCES officer_attachment_messages(id),ordinal INTEGER NOT NULL,
      source_id TEXT UNIQUE NOT NULL REFERENCES files(id),source_generation INTEGER NOT NULL,
      access_generation INTEGER NOT NULL,identity TEXT NOT NULL,digest TEXT NOT NULL,
      media TEXT NOT NULL,bytes INTEGER NOT NULL,name TEXT NOT NULL,
      preparation_kind TEXT NOT NULL CHECK({check}),PRIMARY KEY(message_id,ordinal)) WITHOUT ROWID'''


INDEXES = {'file_draft': 'CREATE INDEX file_draft ON files(draft_id,id)',
           'file_cleanup': 'CREATE INDEX file_cleanup ON files(needs_cleanup,id)'}


def normalized(sql):
    """Ignore formatting outside quotes, never change a SQL literal's value.

    SQLite's table rename quotes these two known identifiers. Only those exact
    double-quoted names may lose identifier quotes; other quoted tokens remain
    byte-exact, including escaped quotes, whitespace and case.
    """
    result, index = [], 0
    while index < len(sql):
        char = sql[index]
        if char in ("'", '"', '`', '['):
            end = ']' if char == '[' else char
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == end:
                    index += 1
                    if char != '[' and index < len(sql) and sql[index] == end:
                        index += 1
                        continue
                    break
                index += 1
            token = sql[start:index]
            if token in ('"files"', '"officer_message_files"'):
                token = token[1:-1]
            result.append(token)
        else:
            if not char.isspace():
                result.append(char.lower())
            index += 1
    return ''.join(result)


def verify_schema(db, *, direct):
    for table, fields, statement in (('files', FILE_FIELDS, files_sql('files', direct=direct)),
                                     ('officer_message_files', MEMBER_FIELDS, members_sql('officer_message_files', direct=direct))):
        row = db.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()
        if (row is None or normalized(row[0]) != normalized(statement)
                or [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')] != fields):
            fail('schema_unavailable', 'Motet attachment storage needs a compatible schema.', 503)
    actual = {r[0]: normalized(r[1]) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name IN ('files','officer_message_files') AND sql IS NOT NULL")}
    if (actual != {name: normalized(sql) for name, sql in INDEXES.items()}
            or db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') AND tbl_name IN ('files','officer_message_files')").fetchone()):
        fail('schema_unavailable', 'Motet attachment indexes could not be verified.', 503)


def migrate(store):
    """Run only during unpublished startup, outside any existing transaction.

    A full database can refuse migration. Rollback retains schema 7, all child
    references and cleanup slots; never raise the cap to make a rebuild fit.
    """
    db = store.db
    if (db.in_transaction or db.execute('PRAGMA foreign_keys').fetchone()[0] != 1
            or db.execute('PRAGMA user_version').fetchone()[0] != 7):
        fail('schema_unavailable', 'Motet migration requires isolated startup.', 503)
    verify_schema(db, direct=False)
    if db.execute('PRAGMA foreign_key_check').fetchone():
        fail('schema_unavailable', 'Attachment references could not be verified.', 503)
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        if db.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
            fail('schema_unavailable', 'Motet migration could not be isolated.', 503)
        db.execute('BEGIN IMMEDIATE')
        for table, fields, statement in (
                ('files', ['rowid'] + FILE_FIELDS, files_sql('files_v8', direct=True)),
                ('officer_message_files', MEMBER_FIELDS, members_sql('officer_message_files_v8', direct=True))):
            db.execute(statement)
            names = ','.join(fields)
            db.execute('INSERT INTO ' + table + '_v8(' + names + ') SELECT ' + names + ' FROM ' + table)
            store.checkpoint('officer_migration_copied_' + table)
        for table in ('files', 'officer_message_files'):
            db.execute('DROP TABLE ' + table)
            store.checkpoint('officer_migration_dropped_' + table)
            db.execute('ALTER TABLE ' + table + '_v8 RENAME TO ' + table)
            store.checkpoint('officer_migration_renamed_' + table)
        for statement in INDEXES.values():
            db.execute(statement)
        verify_schema(db, direct=True)
        if db.execute('PRAGMA foreign_key_check').fetchone():
            fail('schema_unavailable', 'Attachment references changed during migration.', 503)
        from attachments_deletion_slots import verify_all
        verify_all(store)
        db.execute('PRAGMA user_version=8')
        store.checkpoint('officer_migration_before_commit')
        store.files.verify_database()
        db.execute('COMMIT')
    finally:
        if db.in_transaction:
            db.execute('ROLLBACK')
        db.execute('PRAGMA foreign_keys=ON')
        if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            fail('schema_unavailable', 'Attachment reference enforcement is unavailable.', 503)
    if db.execute('PRAGMA foreign_key_check').fetchone():
        fail('schema_unavailable', 'Attachment references could not be restored.', 503)
