import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

from app.services.frozen_context_retrieval import complete_context


class FrozenContextTests(unittest.TestCase):
    def messages(self):
        return [{"role": "system", "content": "[1] Original evidence\n" + "baseline " * 900},
                {"role": "user", "content": "What about Acme?"},
                {"role": "assistant", "content": "Historical answer"},
                {"role": "user", "content": "What is Acme revenue?"}]

    def document(self, text, **kw):
        return dict(text=text, source="acme.txt", provenance={"kind": "runtime_citation"}, **kw)

    def test_history_and_original_labels_are_immutable(self):
        messages = self.messages()
        frozen = copy.deepcopy(messages)
        result = complete_context(question="Acme revenue", messages=messages,
                                  documents=[self.document("Acme revenue was 12 million.\n", original_label=1)])
        self.assertEqual(messages, frozen)
        self.assertEqual(result["messages"][0], frozen[0])
        self.assertEqual(result["messages"][2:], frozen[1:])
        self.assertIn("[1] 来源", result["messages"][1]["content"])
        self.assertLessEqual(result["added_chars"], result["budget_chars"])

    def test_table_header_and_exact_row(self):
        text = "# FY2021, dollars millions\nEntity\tRevenue\tLiabilities\n---\t---\t---\nAcme\t12.0123\t3\nOther\t99\t4\n"
        result = complete_context(question="Acme liabilities FY2021", messages=self.messages(), documents=[self.document(text)])
        output = result["messages"][1]["content"]
        self.assertIn("Entity\tRevenue\tLiabilities", output)
        self.assertIn("Acme\t12.0123\t3", output)
        for record in result["selected"]:
            for span in record["spans"]:
                self.assertIn(text[span["start"]:span["end"]], output)

    def test_missing_or_unauthorized_sources_never_invented(self):
        result = complete_context(question="Acme revenue", messages=self.messages(), documents=[])
        self.assertEqual(result["messages"], self.messages())
        self.assertTrue(result["unresolved"])
        result = complete_context(question="Acme revenue", messages=self.messages(), documents=[dict(text="Acme revenue 999", source="bad")])
        self.assertEqual(result["added_chars"], 0)

    def test_latest_closure_stays_in_supplied_thread(self):
        text = "Acme TLS: initial plan investigate.\n\nAcme TLS final closure: owner Ana will validate certificate renewal.\n"
        unrelated = dict(text="Unrelated final closure: owner Bob will update pricing.", source="pricing.txt", provenance={"kind": "runtime_citation"})
        result = complete_context(question="Acme TLS final closure follow-ups", messages=self.messages(), documents=[self.document(text), unrelated])
        self.assertIn("final closure: owner Ana", result["messages"][1]["content"])
        self.assertNotIn("Unrelated", str(result))

    def test_no_expected_answer_or_case_id_api(self):
        with self.assertRaises(TypeError):
            complete_context(question="x", messages=[], documents=[], gold="secret")
        with self.assertRaises(TypeError):
            complete_context(question="x", messages=[], documents=[], item_id="special")

    def test_no_partial_paragraph_when_budget_too_small(self):
        result = complete_context(question="Acme", messages=self.messages(), documents=[self.document("Acme " * 1000)], max_extra_chars=300)
        self.assertEqual(result["added_chars"], 0)

    def test_runtime_resolver_uses_source_identity_not_catalog_item_mapping(self):
        path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_glm40_context_candidate.py"
        spec = importlib.util.spec_from_file_location("context_prepare", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        catalog = module.catalog_records({"files": [{"path": "/source/acme.txt", "local_path": "unused", "local_sha256": "hash", "item_ids": ["forbidden-mapping"]}]})
        self.assertNotIn("item_ids", catalog[0])
        docs, unresolved = module.resolve_documents(question="revenue", messages=[], citations=[{"source_metadata": {"source_sha256": "hash"}, "fileName": "wrong.txt"}], catalog=catalog, cache={"/source/acme.txt": ("Acme revenue 12", "test", "hash")})
        self.assertEqual(docs[0]["provenance"]["kind"], "runtime_citation")
        self.assertEqual(unresolved, [])

    def test_zero_anchor_followup_uses_prior_user_salient_term(self):
        path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_glm40_context_candidate.py"
        spec = importlib.util.spec_from_file_location("context_prepare", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        catalog = [{"source": "/source/fruit.txt", "name": "fruit.txt", "local_path": "unused", "sha256": "hash"}]
        docs, _ = module.resolve_documents(question="What was its preparation yield factor?", messages=[{"role": "user", "content": "What about avocado?"}], citations=[], catalog=catalog, cache={"/source/fruit.txt": ("Avocado yield 0.733", "test", "hash")})
        self.assertEqual(docs[0]["provenance"]["salient_matches"], ["avocado"])
        docs, _ = module.resolve_documents(question="What was its preparation yield factor?", messages=[], citations=[], catalog=catalog, cache={"/source/fruit.txt": ("Avocado yield 0.733", "test", "hash")})
        self.assertEqual(docs, [])


if __name__ == "__main__":
    unittest.main()
