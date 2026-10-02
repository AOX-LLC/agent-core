"""Image and PDF attachments for model calls.

An attachment's type comes from its first bytes, never from a file name. Only
its media type, SHA-256 and size are ever recorded or hashed; the bytes stay in
memory for the one request that sends them.
"""

import hashlib
import os
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import Field

from aox_agent_core._model import FrozenModel, Sha256Hex
from aox_agent_core.errors import AttachmentError

AttachmentMediaType = Literal["image/png", "image/jpeg", "application/pdf"]

# The API accepts images up to 5 MB each. A PDF's base64 body must fit the
# 32 MB request limit, so its raw size is capped at 24 MB.
MAX_IMAGE_BYTES: Final = 5_000_000
MAX_PDF_BYTES: Final = 24_000_000

_SIGNATURES: Final[tuple[tuple[bytes, AttachmentMediaType], ...]] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)


class Attachment(FrozenModel):
    """A PNG, JPEG or PDF to send with a call.

    Build one with from_bytes() or from_path(), which check the type and size.
    `data` is excluded from every dump, so it never reaches a recording, a hash
    or a log; an attachment read back from a recording has no data.
    """

    media_type: AttachmentMediaType
    sha256: Sha256Hex
    size_bytes: Annotated[int, Field(gt=0)]
    data: bytes | None = Field(default=None, exclude=True, repr=False)

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        media_type: AttachmentMediaType | None = None,
        max_bytes: int | None = None,
    ) -> Self:
        """Check the bytes' type by their signature and their size against the cap.

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
        return cls(
            media_type=sniffed,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            data=data,
        )

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        *,
        media_type: AttachmentMediaType | None = None,
        max_bytes: int | None = None,
    ) -> Self:
        """Read a file and build an attachment from its bytes, as from_bytes() does."""
        try:
            data = Path(path).read_bytes()
        except OSError as error:
            raise AttachmentError(f"Cannot read attachment {path}: {error.strerror}") from error
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
