"""Explicit old-schema reconstruction for migration tests, never production recovery."""
def drop_direct_schema(db):
    """Reconstruct schema7 CHECKs without changing rowids, children or slots."""
    drop_document_schema(db)
    from attachments_officer_migration import members_sql, MEMBER_FIELDS
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='files'").fetchone()[0]
    if 'officer_direct' not in sql:
        return
    assert db.execute("SELECT COUNT(*) FROM files WHERE preparation_kind='officer_direct'").fetchone()[0] == 0
    values = db.execute('SELECT id,preparation_kind FROM files').fetchall()
    db.execute('ALTER TABLE files DROP COLUMN preparation_kind')
    db.execute("ALTER TABLE files ADD COLUMN preparation_kind TEXT NOT NULL DEFAULT 'text' CHECK(preparation_kind IN ('text','original_relay'))")
    db.executemany('UPDATE files SET preparation_kind=? WHERE id=?', [(r[1],r[0]) for r in values])
    db.execute(members_sql('officer_message_files_v7', direct=False))
    names = ','.join(MEMBER_FIELDS)
    db.execute('INSERT INTO officer_message_files_v7('+names+') SELECT '+names+' FROM officer_message_files')
    db.execute('DROP TABLE officer_message_files')
    db.execute('ALTER TABLE officer_message_files_v7 RENAME TO officer_message_files')


def drop_deletion_schema(db):
    """Reconstruct schema 6 before any older fixture reconstruction."""
    drop_direct_schema(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='deletion_source_slots'").fetchone():
        db.execute('DROP TABLE deletion_copy_slots')
        db.execute('DROP TABLE deletion_source_slots')
        db.execute("DELETE FROM meta WHERE key='deletion_cursor'")
        db.execute('DROP INDEX file_draft')
        db.execute('CREATE INDEX file_draft ON files(draft_id,member,id)')


def drop_officer_schema(db):
    drop_deletion_schema(db)
    assert db.execute('SELECT COUNT(*) FROM officer_attachment_messages').fetchone()[0] == 0
    db.execute('DROP TRIGGER draft_admission_insert')
    db.execute('DROP TRIGGER draft_admission_update')
    db.execute('DROP INDEX draft_listing')
    db.execute('ALTER TABLE drafts DROP COLUMN admitted_message')
    db.execute('ALTER TABLE drafts DROP COLUMN admitted_kind')
    db.execute("CREATE INDEX draft_listing ON drafts(destination_id,scope_id,created,id) WHERE state='active' AND admitted_request IS NULL")
    for table in ('conversation_files','officer_message_files','officer_attachment_messages'):
        db.execute('DROP TABLE '+table)


def drop_document_schema(db):
    db.execute('DROP TABLE IF EXISTS officer_document_intents')
    """Reconstruct schema8 only when no execution requests have been admitted."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='officer_document_requests'").fetchone():
        return
    assert db.execute('SELECT COUNT(*) FROM officer_document_requests').fetchone()[0] == 0
    for table in ('officer_document_dependencies','officer_document_requests','officer_document_scopes'):
        db.execute('DROP TABLE '+table)
