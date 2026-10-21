import tempfile
import unittest
from pathlib import Path
from PIL import Image

from image_library import ImageLibrary
from studio_exports import sync_reference_export


class FakeFlow:
    def __init__(self, run): self.run = run
    def get(self, _): return self.run


class ExportTests(unittest.TestCase):
    def test_current_references_are_exported_by_reference_id(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / 'source.png'
            Image.new('RGB', (8, 8), 'red').save(source)
            library = ImageLibrary(root / 'library')
            item = library.import_path(source)
            artifact = {'id': 'artifact_one', 'content': {'image_id': item['id']}}
            run = {'id': 'film_one', 'title': 'Film', 'images': {'MIRA': artifact['id']},
                   'artifacts': [artifact]}
            folder = sync_reference_export(FakeFlow(run), library, run['id'], root / 'exports')
            self.assertTrue((folder / 'MIRA.png').is_file())
            self.assertIn('MIRA.png', (folder / 'manifest.json').read_text(encoding='utf-8'))


if __name__ == '__main__': unittest.main()
