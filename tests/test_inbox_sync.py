"""sync_inbox no longer silently drops non-text files (the HIGH 'PDFs/DOCX rot
forever' hole). PDFs route through the PDF text path; everything else is
quarantined to raw/inbox/unsupported/ (visible, not re-scanned); archival is
collision-safe."""


def test_sync_inbox_quarantines_unsupported_type(kb_root):
    inbox = kb_root.RAW_INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    doc = inbox / 'requirements.docx'
    doc.write_bytes(b'PK\x03\x04 fake docx')

    results = kb_root.sync_inbox()

    assert len(results) == 1
    assert results[0]['status'] == 'unsupported'
    assert not doc.exists()                                   # not silently left to rot
    assert (inbox / 'unsupported' / 'requirements.docx').exists()
    assert len(kb_root.load_manifest()['sources']) == 0        # not ingested as a source


def test_sync_inbox_routes_pdf_through_pdf_path(kb_root, monkeypatch):
    inbox = kb_root.RAW_INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / 'handover.pdf').write_bytes(b'%PDF-1.4 fake')
    monkeypatch.setattr(kb_root, 'extract_pdf_text', lambda _p: '## Page 1\nhandover content')

    results = kb_root.sync_inbox()

    assert len(results) == 1 and results[0].get('status') != 'unsupported'
    assert not (inbox / 'handover.pdf').exists()
    assert (inbox / 'processed' / 'handover.pdf').exists()
    sources = kb_root.load_manifest()['sources']
    assert len(sources) == 1 and sources[0]['source_type'] == 'pdf'


def test_sync_inbox_quarantines_pdf_on_extraction_failure(kb_root, monkeypatch):
    inbox = kb_root.RAW_INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / 'broken.pdf').write_bytes(b'%PDF-1.4 broken')

    def boom(_):
        raise RuntimeError('No extractable text')
    monkeypatch.setattr(kb_root, 'extract_pdf_text', boom)

    results = kb_root.sync_inbox()

    assert results[0]['status'] == 'unsupported'
    assert (inbox / 'unsupported' / 'broken.pdf').exists()
    assert len(kb_root.load_manifest()['sources']) == 0


def test_sync_inbox_ingests_files_from_nested_subdirs(kb_root):
    inbox = kb_root.RAW_INBOX_DIR
    (inbox / 'acme' / 'repos').mkdir(parents=True, exist_ok=True)
    (inbox / 'acme' / 'repos' / 'notes.md').write_text('repo notes')
    (inbox / 'top.md').write_text('top level')

    results = kb_root.sync_inbox()

    assert len(results) == 2
    assert len(kb_root.load_manifest()['sources']) == 2          # distinct names -> distinct sources


def test_sync_inbox_archival_does_not_clobber_same_named_files(kb_root):
    # Two same-named files in different subdirs must not overwrite each other in
    # processed/ (the recursive-rglob + flat-archive data-loss path). This is the
    # file-level guarantee; the manifest-level source_id collision on identical
    # title+second is a separate, pre-existing issue.
    inbox = kb_root.RAW_INBOX_DIR
    (inbox / 'a').mkdir(parents=True, exist_ok=True)
    (inbox / 'b').mkdir(parents=True, exist_ok=True)
    (inbox / 'a' / 'README.md').write_text('from a')
    (inbox / 'b' / 'README.md').write_text('from b')

    kb_root.sync_inbox()

    processed = inbox / 'processed'
    names = sorted(p.name for p in processed.glob('*.md'))
    assert names == ['README-1.md', 'README.md']                 # neither clobbered
    assert sorted((processed / n).read_text() for n in names) == ['from a', 'from b']
