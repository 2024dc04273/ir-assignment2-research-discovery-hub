from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import app


class IRCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_index_path = app.INDEX_PATH
        app.INDEX_PATH = Path(self.temp_dir.name) / "index.json"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        app.initialise_database(self.conn)
        outcomes = app.ingest_items(
            self.conn, app.parse_csv_items(app.DATA_DIR / "sample_documents.csv")
        )
        self.assertEqual(outcomes["added"], 15)
        app.seed_demo_links(self.conn)
        self.index = app.build_index(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        app.INDEX_PATH = self.original_index_path
        self.temp_dir.cleanup()

    def test_demo_corpus_and_index_are_reproducible(self) -> None:
        self.assertEqual(self.index["document_count"], 15)
        self.assertEqual(len(self.index["postings"]), 395)
        self.assertAlmostEqual(self.index["average_length"], 48.333333, places=5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 30)

    def test_boolean_phrase_or_and_negative_only_queries(self) -> None:
        docs = app.load_documents(self.conn)
        candidates = app.boolean_candidates(
            '"information retrieval" OR image', self.index["postings"], docs
        )
        titles = {doc["title"] for doc in docs if doc["doc_id"] in candidates}
        self.assertIn("Information Retrieval Foundations", titles)
        self.assertIn("Image and Visual Retrieval", titles)

        negative_results = app.search("NOT ranking", self.conn, top_k=20, rank_weight=0.2)
        self.assertTrue(negative_results)
        self.assertTrue(all("ranking" not in item["raw_text"].lower() for item in negative_results))

    def test_duplicate_and_invalid_urls_are_rejected(self) -> None:
        first = app.parse_csv_items(app.DATA_DIR / "sample_documents.csv")[0]
        self.assertEqual(app.add_document(self.conn, first)[0], "duplicate_url")
        invalid = dict(first, url="file:///etc/passwd", raw_text="A sufficiently long invalid document body for testing.")
        self.assertEqual(app.add_document(self.conn, invalid)[0], "invalid_url")

    def test_standard_metrics_use_full_ranking_and_cutoff_k(self) -> None:
        values = app.metrics_for_ranking(
            ["a", "x", "b"], {"a": 1.0, "b": 1.0}, k=2
        )
        self.assertAlmostEqual(values["Precision"], 2 / 3)
        self.assertAlmostEqual(values["Recall"], 1.0)
        self.assertAlmostEqual(values["F1"], 0.8)
        self.assertAlmostEqual(values["Precision@K"], 0.5)
        self.assertAlmostEqual(values["Recall@K"], 0.5)
        self.assertAlmostEqual(values["AP"], (1 + 2 / 3) / 2)
        self.assertAlmostEqual(values["MRR"], 1.0)

    def test_evaluation_and_feedback_aware_recommendations(self) -> None:
        qrels = pd.read_csv(app.DATA_DIR / "sample_qrels.csv")
        detailed, summary = app.evaluation_table(self.conn, qrels, 5)
        self.assertEqual(len(detailed), 10)
        self.assertEqual(set(summary["Strategy"]), {"BM25", "BM25 + PageRank"})
        scores = summary.set_index("Strategy")
        self.assertGreater(
            scores.loc["BM25 + PageRank", "NDCG@K"], scores.loc["BM25", "NDCG@K"]
        )

        self.conn.execute(
            "INSERT INTO feedback(user_id, doc_id, score, created_at) VALUES (?, ?, ?, ?)",
            ("tester", 2, -1, "2026-01-01T00:00:00+00:00"),
        )
        self.conn.commit()
        values = app.recommendations(self.conn, selected_id=1, user_id="tester", top_k=14)
        self.assertNotIn(2, {item["doc_id"] for item in values})


if __name__ == "__main__":
    unittest.main()
