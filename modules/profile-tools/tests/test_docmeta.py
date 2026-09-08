import tempfile
import unittest
from pathlib import Path
import importlib.util

from pypdf import PdfReader, PdfWriter
spec = importlib.util.spec_from_file_location('client_docmeta', Path(__file__).resolve().parents[1] / 'clean_docmeta.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.AUTHOR = AUTHOR = 'Test Owner'
clean_pdf = module.clean_pdf


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_pdf(self, author='python-docx', encrypted=False):
        path = self.root / 'test.pdf'
        writer = PdfWriter()
        writer.pdf_header = '%PDF-1.7'
        writer.add_blank_page(612, 792)
        writer.add_metadata({'/Author': author, '/Title': 'Test', '/Producer': 'LibreOffice 24.2'})
        if encrypted:
            writer.encrypt('synthetic-test-password')
        writer.write(path)
        return path

    def test_generated_author_only_and_idempotent(self):
        path = self.make_pdf()
        self.assertTrue(clean_pdf(path))
        reader = PdfReader(path)
        self.assertEqual(reader.metadata.author, AUTHOR)
        self.assertEqual(reader.metadata.title, 'Test')
        self.assertEqual(reader.metadata.producer, 'LibreOffice 24.2')
        self.assertEqual(reader.pdf_header, '%PDF-1.7')
        self.assertEqual(len(reader.pages), 1)
        self.assertFalse(clean_pdf(path))

    def test_human_author_untouched(self):
        path = self.make_pdf('Original author')
        before = path.read_bytes()
        self.assertFalse(clean_pdf(path))
        self.assertEqual(path.read_bytes(), before)

    def test_encrypted_untouched(self):
        path = self.make_pdf(encrypted=True)
        before = path.read_bytes()
        self.assertFalse(clean_pdf(path))
        self.assertEqual(path.read_bytes(), before)

    def test_symlink_untouched(self):
        path = self.make_pdf()
        link = self.root / 'link.pdf'
        link.symlink_to(path)
        self.assertFalse(clean_pdf(link))
        self.assertEqual(PdfReader(path).metadata.author, 'python-docx')


if __name__ == '__main__':
    unittest.main()
