"""Image and PDF attachments for model calls.

An attachment's type comes from its first bytes, never from a file name. Only
its media type, SHA-256 and size are ever recorded or hashed; the bytes stay in
memory for the one request that sends them.
"""

import hashlib
import os
import re
import stat
import zlib
from typing import Annotated, Final, Literal, Self

from pydantic import Field, PrivateAttr, ValidationInfo, model_validator

from aox_agent_core._model import FrozenModel, Sha256Hex
from aox_agent_core._validation import HASHED_ATTACHMENT
from aox_agent_core.errors import AttachmentError

AttachmentMediaType = Literal["image/png", "image/jpeg", "application/pdf"]

# The API accepts images up to 5 MB each. A PDF's base64 body must fit the
# 32 MB request limit, so its raw size is capped at 24 MB.
MAX_IMAGE_BYTES: Final = 5_000_000
MAX_PDF_BYTES: Final = 24_000_000

# Counting a PDF's pages gives up past this many inflated bytes or object streams.
MAX_INFLATED_BYTES: Final = 16_000_000
MAX_OBJECT_STREAMS: Final = 10_000

_PDF_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
_PDF_PAGE_COUNT = re.compile(rb"/Count\s+(\d{1,9})")
_PDF_ESCAPED_TYPE = re.compile(rb"/Type\s*/[^\s/<>\[\]()]*#")
_PDF_OBJECT_STREAM = re.compile(rb"/Type\s*/ObjStm(?![a-zA-Z])")
_PDF_STREAM_START = re.compile(rb"stream\r?\n")

_SIGNATURES: Final[tuple[tuple[bytes, AttachmentMediaType], ...]] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)


class Attachment(FrozenModel):
    """A PNG, JPEG or PDF to send with a call.

    Build one with from_bytes() or from_path(). An attachment built directly
    with its bytes is checked the same way: its type, size and SHA-256 must
    match the bytes, or AttachmentError is raised. `data` is excluded from every
    dump, so it never reaches a recording, a hash or a log; an attachment read
    back from a recording has no data.

    model_copy(update=...) skips those checks, as it does for every pydantic
    model: never use it to change `data`. Equality also compares the page count
    read from the bytes, so compare `sha256` to tell whether two attachments
    hold the same file.
    """

    media_type: AttachmentMediaType
    sha256: Sha256Hex
    size_bytes: Annotated[int, Field(gt=0)]
    data: bytes | None = Field(default=None, exclude=True, repr=False)
    _pdf_pages: int | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _fields_match_the_bytes(self, info: ValidationInfo) -> Self:
        if self.data is None:
            return self
        sniffed = _sniff(self.data)
        if sniffed != self.media_type:
            raise AttachmentError(f"Declared {self.media_type} but the bytes are {sniffed}.")
        if len(self.data) != self.size_bytes:
            raise AttachmentError(
                f"size_bytes is {self.size_bytes} but the attachment is {len(self.data)} bytes."
            )
        cap = _cap_for(sniffed)
        if len(self.data) > cap:
            raise AttachmentError(
                f"The {sniffed} attachment is {len(self.data)} bytes; the cap is {cap}."
            )
        already_hashed = bool(info.context and info.context.get(HASHED_ATTACHMENT))
        if not already_hashed and hashlib.sha256(self.data).hexdigest() != self.sha256:
            raise AttachmentError("sha256 does not match the attachment's bytes.")
        if sniffed == "application/pdf":
            self._pdf_pages = _pdf_page_count(self.data)
        return self

    @property
    def pdf_pages(self) -> int | None:
        """A PDF's page count, read once from its bytes; None for an image, a PDF
        whose pages could not be counted, or an attachment built without bytes."""
        return self._pdf_pages

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        media_type: AttachmentMediaType | None = None,
        max_bytes: int | None = None,
    ) -> Self:
        """Check the bytes' type by their signature and their size against the cap.

        Counting a PDF's pages can take up to about a second of CPU on a file
        built to be slow; in async code, build attachments from untrusted bytes
        with asyncio.to_thread.

        A declared media_type must agree with the signature. max_bytes can only
        lower the per-type cap. Raises AttachmentError.
        """
        sniffed = _sniff(data)
        if media_type is not None and media_type != sniffed:
            raise AttachmentError(f"Declared {media_type} but the bytes are {sniffed}.")
        cap = _cap_for(sniffed)
        if max_bytes is not None:
            cap = min(cap, max_bytes)
        if len(data) > cap:
            raise AttachmentError(
                f"The {sniffed} attachment is {len(data)} bytes; the cap is {cap}."
            )
        return cls.model_validate(
            {
                "media_type": sniffed,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "data": data,
            },
            context={HASHED_ATTACHMENT: True},
        )

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        *,
        media_type: AttachmentMediaType | None = None,
        max_bytes: int | None = None,
    ) -> Self:
        """Read a file and build an attachment from its bytes, as from_bytes() does.

        Only a regular file is read, and never more than one byte past the
        largest cap, so a huge or endless file fails fast.
        """
        limit = MAX_PDF_BYTES if max_bytes is None else min(max_bytes, MAX_PDF_BYTES)
        try:
            # O_NONBLOCK, so opening a FIFO returns at once and is then refused.
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise AttachmentError(f"Attachment {path} is not a regular file.")
                data = file.read(limit + 1)
        except OSError as error:
            raise AttachmentError(f"Cannot read attachment {path}: {error.strerror}") from error
        if len(data) > limit:
            raise AttachmentError(f"Attachment {path} is over the {limit}-byte cap.")
        return cls.from_bytes(data, media_type=media_type, max_bytes=max_bytes)

    def reference(self) -> "Attachment":
        """The same attachment without its bytes: what recordings and keys hold."""
        return self.model_copy(update={"data": None})


def _sniff(data: bytes) -> AttachmentMediaType:
    if not data:
        raise AttachmentError("The attachment is empty.")
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    raise AttachmentError("The attachment is not a PNG, JPEG or PDF.")


def _cap_for(media_type: AttachmentMediaType) -> int:
    return MAX_PDF_BYTES if media_type == "application/pdf" else MAX_IMAGE_BYTES


def _pdf_page_count(data: bytes) -> int | None:
    """Count a PDF's pages: the larger of its page objects and its largest /Count.

    Page objects inside Flate-encoded object streams are counted too. Each byte
    is scanned once. Returns None, so the caller assumes the most pages, when the
    count cannot be trusted: an object stream that is not Flate, more than
    MAX_OBJECT_STREAMS of them, inflating past MAX_INFLATED_BYTES in all, a
    /Type name written with # escapes, or no pages found.
    """
    view = memoryview(data)
    chunks = [data]
    inflated_budget = MAX_INFLATED_BYTES
    position = 0
    while (object_stream := _PDF_OBJECT_STREAM.search(data, position)) is not None:
        if len(chunks) > MAX_OBJECT_STREAMS:
            return None
        start = _PDF_STREAM_START.search(data, object_stream.end())
        end = data.find(b"endstream", start.end()) if start is not None else -1
        if start is None or end < 0:
            return None
        inflater = zlib.decompressobj()
        try:
            # inflated_budget is always positive here; a max_length of 0 means no limit.
            inflated = inflater.decompress(view[start.end() : end], inflated_budget)
        except zlib.error:
            return None
        inflated_budget -= len(inflated)
        if inflater.unconsumed_tail or inflated_budget <= 0:
            return None
        chunks.append(inflated)
        position = end + len(b"endstream")

    page_objects = 0
    declared = 0
    for chunk in chunks:
        if _PDF_ESCAPED_TYPE.search(chunk):
            return None
        page_objects += len(_PDF_PAGE_OBJECT.findall(chunk))
        declared = max([declared, *(int(count) for count in _PDF_PAGE_COUNT.findall(chunk))])
    return max(page_objects, declared) or None
