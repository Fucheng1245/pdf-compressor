from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import hashlib

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          NameObject, NumberObject, TextStringObject)

from pdfcompressor.core import CompressionError, compress


def fixture_pdf(path: Path, image: bool = False, transparent: bool = False) -> Path:
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    fonts = DictionaryObject({NameObject('/F1'): writer._add_object(font)})
    resources = DictionaryObject({NameObject('/Font'): fonts})
    commands = b'BT /F1 16 Tf 40 740 Td (Text remains selectable.) Tj ET\n'
    if image:
        im = Image.effect_noise((2700, 1800), 45).convert('RGB')
        raw = DecodedStreamObject(); raw.set_data(im.tobytes())
        raw.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                    NameObject('/Width'): NumberObject(im.width), NameObject('/Height'): NumberObject(im.height),
                    NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
        if transparent:
            mask = DecodedStreamObject(); mask.set_data(bytes([180]) * (im.width * im.height))
            mask.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                         NameObject('/Width'): NumberObject(im.width), NameObject('/Height'): NumberObject(im.height),
                         NameObject('/ColorSpace'): NameObject('/DeviceGray'), NameObject('/BitsPerComponent'): NumberObject(8)})
            raw[NameObject('/SMask')] = writer._add_object(mask.flate_encode())
        resources[NameObject('/XObject')] = DictionaryObject({NameObject('/Im0'): writer._add_object(raw.flate_encode())})
        commands += b'q 400 0 0 280 40 380 cm /Im0 Do Q\n'
    page[NameObject('/Resources')] = resources
    stream = DecodedStreamObject(); stream.set_data(commands)
    page[NameObject('/Contents')] = writer._add_object(stream)
    # Transitive private payload chain, just like Illustrator editing records.
    private = DecodedStreamObject(); private.set_data(b'private-edit-data' * 50_000)
    data = DictionaryObject({NameObject('/AIPDFPrivateData1'): writer._add_object(private)})
    piece = DictionaryObject({NameObject('/Illustrator'): writer._add_object(data)})
    page[NameObject('/PieceInfo')] = writer._add_object(piece)
    writer.add_metadata({'/Title': 'Test document'})
    writer.add_outline_item('Start', 0)
    writer.add_attachment('notes.txt', b'keep this attachment')
    writer.write(path)
    return path


@pytest.mark.parametrize('preset', ['high', 'mid', 'low'])
def test_preserve_document_and_clean_private_data(tmp_path, preset):
    src = fixture_pdf(tmp_path / '中文 name.pdf')
    digest = hashlib.sha256(src.read_bytes()).digest()
    result = compress(src, preset)
    original, output = PdfReader(src), PdfReader(result.output)
    assert result.output.name == f'中文 name_{preset}.pdf'
    assert hashlib.sha256(src.read_bytes()).digest() == digest
    assert output.pages[0].extract_text() == original.pages[0].extract_text()
    assert '/PieceInfo' not in output.pages[0]
    assert output.metadata.title == 'Test document'
    assert output.outline
    assert output.attachments['notes.txt'] == [b'keep this attachment']
    assert result.after_bytes < result.before_bytes


@pytest.mark.parametrize('preset,edge', [('high',2700),('mid',2400),('low',1600)])
def test_images_and_soft_mask(tmp_path, preset, edge):
    src = fixture_pdf(tmp_path / 'image.pdf', image=True, transparent=True)
    result = compress(src, preset)
    reader = PdfReader(result.output)
    image = reader.pages[0].images[0]
    assert image.image.width == edge
    obj = image.indirect_reference.get_object()
    assert obj['/SMask']['/Width'] == obj['/Width']
    assert obj['/SMask']['/Height'] == obj['/Height']
    assert reader.pages[0].extract_text() == PdfReader(src).pages[0].extract_text()


def test_shared_soft_mask_does_not_change_untouched_image(tmp_path):
    src = fixture_pdf(tmp_path / 'shared.pdf', image=True, transparent=True)
    writer = PdfWriter(clone_from=src)
    page = writer.pages[0]
    first = page['/Resources']['/XObject']['/Im0']
    second = DecodedStreamObject()
    second.set_data(bytes(first['/Width'] * first['/Height'] * 3))
    for key in ('/Type', '/Subtype', '/Width', '/Height', '/ColorSpace', '/BitsPerComponent', '/SMask'):
        second[NameObject(key)] = first.raw_get(key)
    page['/Resources']['/XObject'][NameObject('/Im1')] = writer._add_object(second.flate_encode())
    writer.write(src)
    result = compress(src, 'low')
    objects = PdfReader(result.output).pages[0]['/Resources']['/XObject']
    assert objects['/Im0']['/Width'] == 1600
    assert objects['/Im0']['/SMask']['/Width'] == 1600
    assert objects['/Im1']['/Width'] == 2700
    assert objects['/Im1']['/SMask']['/Width'] == 2700


def test_failed_write_cleans_temporary_file(tmp_path, monkeypatch):
    src = fixture_pdf(tmp_path / 'failure.pdf')
    def broken_write(self, target):
        Path(target).write_bytes(b'incomplete')
        raise OSError('disk write failed')
    monkeypatch.setattr(PdfWriter, 'write', broken_write)
    with pytest.raises(CompressionError, match='disk write failed'):
        compress(src)
    assert not list(tmp_path.glob('.pdfcompress-*'))
    assert not list(tmp_path.glob('*_high.pdf'))


def test_collisions_and_parallel_names(tmp_path):
    src = fixture_pdf(tmp_path / 'main.pdf')
    existing = tmp_path / 'main_high.pdf'; existing.write_bytes(b'keep')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: compress(src, 'high'), range(2)))
    assert {r.output.name for r in results} == {'main_high_2.pdf', 'main_high_3.pdf'}
    assert existing.read_bytes() == b'keep'


def test_encrypted_and_invalid_inputs(tmp_path):
    src = tmp_path / 'encrypted.pdf'
    writer = PdfWriter(); writer.add_blank_page(100, 100); writer.encrypt('secret'); writer.write(src)
    with pytest.raises(CompressionError, match='加密'):
        compress(src)
    src.write_bytes(b'not a pdf')
    with pytest.raises(CompressionError):
        compress(src)
    assert not list(tmp_path.glob('*_high.pdf'))
    assert not list(tmp_path.glob('.pdfcompress-*'))


def test_signature_refused(tmp_path):
    src = tmp_path / 'signed.pdf'; writer = PdfWriter(); writer.add_blank_page(100, 100)
    signature = DictionaryObject({NameObject('/Type'): NameObject('/Sig'), NameObject('/Contents'): TextStringObject('signed')})
    field = DictionaryObject({NameObject('/FT'): NameObject('/Sig'), NameObject('/T'): TextStringObject('Signature'), NameObject('/V'): writer._add_object(signature)})
    writer.root_object[NameObject('/AcroForm')] = DictionaryObject({NameObject('/Fields'): ArrayObject([writer._add_object(field)])})
    writer.write(src)
    with pytest.raises(CompressionError, match='数字签名'):
        compress(src)


def test_no_growth(tmp_path):
    src = tmp_path / 'tiny.pdf'; w=PdfWriter(); w.add_blank_page(100,100); w.write(src)
    result = compress(src)
    assert result.after_bytes <= result.before_bytes
