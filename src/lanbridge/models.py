from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SendJob:
    source_path: Path
    relative_path: str
    size: int
    transfer_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def file_name(self) -> str:
        return self.source_path.name

