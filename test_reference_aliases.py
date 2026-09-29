import unittest
from reference_aliases import resolve_reference_aliases as resolve

class ReferenceAliasTests(unittest.TestCase):
    def test_reorder_changes_index_not_identity(self):
        self.assertEqual(resolve("@meo waves; @gau stays", ["gau", "meo"], 2), "@Image2 waves; @Image1 stays")
        self.assertEqual(resolve("@meo waves; @gau stays", ["meo", "gau"], 2), "@Image1 waves; @Image2 stays")
    def test_exact_tokens_and_email(self):
        self.assertEqual(resolve("@cat @cat2 x@cat.com", ["cat", "cat2"], 2), "@Image1 @Image2 x@cat.com")
    def test_invalid_mapping(self):
        for names, count, prompt in [(["cat", "CAT"], 2, "x"), (["cat"], 2, "x"), (["cat"], 1, "@removed"), (["cat"], 1, "@Image2")]:
            with self.subTest(names=names, prompt=prompt), self.assertRaises(ValueError):
                resolve(prompt, names, count)
    def test_free_names(self):
        self.assertEqual(resolve("@red cat (v2) and @123 and @Image9", ["red cat (v2)", "123", "Image9"], 3), "@Image1 and @Image2 and @Image3")
        self.assertEqual(resolve("@cat @cat hero", ["cat", "cat hero"], 2), "@Image1 @Image2")
        with self.assertRaises(ValueError):
            resolve("x", ["Image2", ""], 2)

    def test_optional_and_native(self):
        self.assertEqual(resolve("ordinary @text", [], 0), "ordinary @text")
        self.assertEqual(resolve("@image2 @CAT", ["cat", ""], 2), "@Image2 @Image1")
if __name__ == "__main__": unittest.main()
