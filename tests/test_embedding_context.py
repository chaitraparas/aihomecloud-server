"""
Enrichment of embedding text from the indexes we already keep.

The failure this guards against is silence: a path-key format mismatch between databases would
produce *no* enrichment and no error, leaving search exactly as bad as before while looking like
the feature shipped.
"""

import sqlite3

import pytest

from app import embedding_context as ec


def _media_db(tmp_path, rows):
    """rows: (rel_path, category, capture_date)"""
    db = tmp_path / "media.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, rel_path TEXT UNIQUE)")
    conn.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, blob_id INTEGER, category TEXT, "
        "capture_date INTEGER, deleted INTEGER NOT NULL DEFAULT 0)"
    )
    for i, (rel_path, category, capture) in enumerate(rows, start=1):
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (i, rel_path))
        conn.execute(
            "INSERT INTO entries (blob_id, category, capture_date, deleted) VALUES (?, ?, ?, 0)",
            (i, category, capture),
        )
    conn.commit()
    conn.close()
    return db


def _docs_db(tmp_path, rows):
    """rows: (path, ocr_text)"""
    db = tmp_path / "docs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE VIRTUAL TABLE doc_index USING fts5(path UNINDEXED, ocr_text)")
    for path, ocr in rows:
        conn.execute("INSERT INTO doc_index (path, ocr_text) VALUES (?, ?)", (path, ocr))
    conn.commit()
    conn.close()
    return db


class TestOcrEnrichment:
    def test_ocr_text_is_attached_to_its_file(self, tmp_path):
        media = _media_db(tmp_path, [])
        docs = _docs_db(tmp_path, [("/p/a.jpg", "NVIDIA Corp NASDAQ NVDA quarterly results")])

        out = ec.extras_for(["/p/a.jpg"], media, docs)

        assert "NVIDIA" in out["/p/a.jpg"]

    def test_ocr_newlines_are_collapsed(self, tmp_path):
        """OCR breaks lines mid-sentence; a sentence model needs it flattened."""
        media = _media_db(tmp_path, [])
        docs = _docs_db(tmp_path, [("/p/a.jpg", "TOTAL NET\n\nWORTH\n INVESTABLE")])

        assert ec.extras_for(["/p/a.jpg"], media, docs)["/p/a.jpg"] == \
            "TOTAL NET WORTH INVESTABLE"

    def test_long_ocr_is_truncated(self, tmp_path):
        media = _media_db(tmp_path, [])
        docs = _docs_db(tmp_path, [("/p/a.jpg", "word " * 5000)])

        assert len(ec.extras_for(["/p/a.jpg"], media, docs)["/p/a.jpg"]) <= ec.MAX_OCR_CHARS

    def test_empty_ocr_contributes_nothing(self, tmp_path):
        media = _media_db(tmp_path, [])
        docs = _docs_db(tmp_path, [("/p/a.jpg", "")])

        assert "/p/a.jpg" not in ec.extras_for(["/p/a.jpg"], media, docs)


class TestMediaEnrichment:
    def test_capture_date_becomes_a_searchable_month(self, tmp_path):
        media = _media_db(tmp_path, [("/p/a.jpg", "Photos", 1777365251)])
        docs = _docs_db(tmp_path, [])

        text = ec.extras_for(["/p/a.jpg"], media, docs)["/p/a.jpg"]

        assert "2026" in text
        assert "photo" in text

    def test_category_others_adds_no_noise_word(self, tmp_path):
        media = _media_db(tmp_path, [("/p/a.bin", "Others", None)])
        docs = _docs_db(tmp_path, [])

        assert "/p/a.bin" not in ec.extras_for(["/p/a.bin"], media, docs)

    def test_deleted_entries_are_ignored(self, tmp_path):
        media = _media_db(tmp_path, [("/p/a.jpg", "Photos", 1777365251)])
        conn = sqlite3.connect(media)
        conn.execute("UPDATE entries SET deleted = 1")
        conn.commit()
        conn.close()
        docs = _docs_db(tmp_path, [])

        assert ec.extras_for(["/p/a.jpg"], media, docs) == {}

    def test_a_bad_timestamp_does_not_raise(self, tmp_path):
        media = _media_db(tmp_path, [("/p/a.jpg", "Photos", 99999999999999)])
        docs = _docs_db(tmp_path, [])

        ec.extras_for(["/p/a.jpg"], media, docs)  # must not raise


class TestCombiningAndDegradation:
    def test_media_and_ocr_both_appear(self, tmp_path):
        media = _media_db(tmp_path, [("/p/a.jpg", "Photos", 1777365251)])
        docs = _docs_db(tmp_path, [("/p/a.jpg", "NASDAQ NVDA")])

        text = ec.extras_for(["/p/a.jpg"], media, docs)["/p/a.jpg"]

        assert "photo" in text and "NASDAQ" in text

    def test_missing_databases_degrade_to_nothing(self, tmp_path):
        out = ec.extras_for(["/p/a.jpg"], tmp_path / "nope.db", tmp_path / "also-nope.db")

        assert out == {}

    def test_a_path_in_neither_index_is_simply_absent(self, tmp_path):
        media = _media_db(tmp_path, [("/p/a.jpg", "Photos", 1777365251)])
        docs = _docs_db(tmp_path, [])

        assert "/p/unknown.jpg" not in ec.extras_for(["/p/unknown.jpg"], media, docs)

    def test_more_paths_than_one_sql_chunk(self, tmp_path):
        """SQLite caps bound variables; the batching must not drop the tail."""
        rows = [(f"/p/{i}.jpg", "Photos", 1777365251) for i in range(ec._SQL_CHUNK * 2 + 7)]
        media = _media_db(tmp_path, rows)
        docs = _docs_db(tmp_path, [])

        out = ec.extras_for([r[0] for r in rows], media, docs)

        assert len(out) == len(rows)

    def test_empty_input(self, tmp_path):
        assert ec.extras_for([], tmp_path / "m.db", tmp_path / "d.db") == {}
