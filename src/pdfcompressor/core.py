from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject


@dataclass(frozen=True)
class Preset:
    label: str
    max_edge: int | None
    jpeg_quality: int | None


PRESETS = {
    "high": Preset("画质优先", None, None),
    "mid": Preset("均衡压缩", 2400, 85),
    "low": Preset("体积优先", 1600, 72),
}


class CompressionError(Exception):
    """An input cannot be safely processed."""


@dataclass(frozen=True)
class Result:
    source: Path
    output: Path
    preset: str
    before_bytes: int
    after_bytes: int
    pages: int
    images_reencoded: int
    skipped_images: int

    def as_dict(self) -> dict:
        return {**self.__dict__, "source": str(self.source), "output": str(self.output)}


def _clean_private_data(writer: PdfWriter) -> None:
    # Illustrator editing payloads are not needed to render the PDF. Preserve
    # attachments, annotations, document metadata and every visible resource.
    for obj in writer._objects:
        if isinstance(obj, DictionaryObject):
            obj.pop(NameObject("/PieceInfo"), None)
            for key in list(obj):
                if str(key).startswith("/AIPDFPrivateData"):
                    del obj[key]
    # pypdf's orphan removal is not transitive in one pass: private dictionaries
    # can reference other dictionaries which in turn reference large streams.
    while True:
        before = sum(obj is not None for obj in writer._objects)
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
        after = sum(obj is not None for obj in writer._objects)
        if after == before:
            break
    for index, obj in enumerate(writer._objects):
        if hasattr(obj, "_data") and not obj.get("/Filter"):
            compressed = obj.flate_encode(level=9)
            if len(compressed._data) < len(obj._data):
                compressed.indirect_reference = obj.indirect_reference
                writer._objects[index] = compressed


def _compress_images(writer: PdfWriter, preset: Preset) -> tuple[int, int]:
    if preset.max_edge is None:
        return 0, 0
    seen: set[int] = set()
    count = skipped = 0
    for page in writer.pages:
        # A damaged/unsupported image may fail when pypdf enumerates it. Leave
        # that resource untouched; do not risk changing the document to fix it.
        try:
            keys = list(page.images.keys())
        except Exception:
            skipped += 1
            continue
        for key in keys:
            try:
                image = page.images[key]
                ref = image.indirect_reference
                if ref is None or ref.idnum in seen:
                    continue
                seen.add(ref.idnum)
                obj = ref.get_object()
                original = image.image
                if original.width * original.height < 100_000:
                    continue
                # Color-key masks and explicit masks need special treatment;
                # leave them unchanged. Soft masks are supported below.
                if "/Mask" in obj:
                    skipped += 1
                    continue
                rgb = original.convert("RGB")
                rgb.thumbnail((preset.max_edge, preset.max_edge), Image.Resampling.LANCZOS)
                encoded = BytesIO()
                rgb.save(encoded, format="JPEG", quality=preset.jpeg_quality,
                         subsampling=0, optimize=True)
                data = encoded.getvalue()
                if len(data) >= len(obj._data):
                    continue
                mask_ref = None
                if "/SMask" in obj:
                    if original.mode != "RGBA":
                        skipped += 1
                        continue
                    alpha = original.getchannel("A").resize(rgb.size, Image.Resampling.LANCZOS)
                    mask = DecodedStreamObject()
                    mask.set_data(alpha.tobytes())
                    mask.update({NameObject("/Type"): NameObject("/XObject"),
                                 NameObject("/Subtype"): NameObject("/Image"),
                                 NameObject("/Width"): NumberObject(rgb.width),
                                 NameObject("/Height"): NumberObject(rgb.height),
                                 NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                                 NameObject("/BitsPerComponent"): NumberObject(8)})
                    # New mask object: a shared original mask must never be
                    # resized in place, since other images may still use it.
                    mask_ref = writer._add_object(mask.flate_encode(level=9))
                obj._data = data
                obj.decoded_self = None
                obj.update({NameObject("/Filter"): NameObject("/DCTDecode"),
                            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                            NameObject("/BitsPerComponent"): NumberObject(8),
                            NameObject("/Width"): NumberObject(rgb.width),
                            NameObject("/Height"): NumberObject(rgb.height)})
                for field in ("/DecodeParms", "/Decode"):
                    obj.pop(NameObject(field), None)
                if mask_ref is not None:
                    obj[NameObject("/SMask")] = mask_ref
                count += 1
            except Exception:
                skipped += 1
    return count, skipped


def _publish(temp: Path, source: Path, preset: str) -> Path:
    # Reserve the final name with O_EXCL so concurrent invocations cannot
    # overwrite each other, existing files or symlinks.
    index = 1
    while True:
        suffix = "" if index == 1 else f"_{index}"
        output = source.with_name(f"{source.stem}_{preset}{suffix}.pdf")
        try:
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            index += 1
            continue
        os.close(fd)
        try:
            os.replace(temp, output)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        return output


def compress(source: str | Path, preset: str = "high") -> Result:
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    source = Path(source).expanduser().absolute()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise CompressionError("请选择现有的 PDF 文件。")
    temporary: Path | None = None
    try:
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise CompressionError("暂不处理加密 PDF，请先保存未加密副本。")
        fields = reader.get_fields() or {}
        if any(field.get("/FT") == "/Sig" and field.get("/V") for field in fields.values()):
            raise CompressionError("此 PDF 带有数字签名，重新保存会使签名失效，已跳过。")
        if not len(reader.pages):
            raise CompressionError("PDF 没有页面。")
        before = source.stat().st_size
        writer = PdfWriter(clone_from=reader)
        changed, skipped = _compress_images(writer, PRESETS[preset])
        _clean_private_data(writer)
        fd, path = tempfile.mkstemp(prefix=".pdfcompress-", suffix=".tmp", dir=source.parent)
        os.close(fd)
        temporary = Path(path)
        writer.write(temporary)
        check = PdfReader(temporary)
        if len(check.pages) != len(reader.pages):
            raise CompressionError("输出页面校验失败，未保存副本。")
        # Already optimized inputs may grow. Save an unchanged copy instead.
        if temporary.stat().st_size >= before:
            shutil.copyfile(source, temporary)
            changed = 0
        after = temporary.stat().st_size
        output = _publish(temporary, source, preset)
        temporary = None
        return Result(source, output, preset, before, after, len(reader.pages), changed, skipped)
    except CompressionError:
        raise
    except Exception as exc:
        raise CompressionError(f"无法压缩此 PDF：{exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
