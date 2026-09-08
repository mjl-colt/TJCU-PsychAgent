from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


_PROMPT_NAME = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


@dataclass(frozen=True)
class PromptAsset:
    name: str
    version: str
    description: str
    body: str
    sha256: str

    @property
    def prompt_id(self) -> str:
        return f"{self.name}:{self.version}"


class PromptCatalog:
    """Loads reviewed prompt assets with stable IDs and strict placeholders."""

    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[1] / "prompts"

    @lru_cache(maxsize=32)
    def get_required(self, name: str) -> PromptAsset:
        if not _PROMPT_NAME.fullmatch(name):
            raise ValueError(f"invalid prompt name: {name!r}")
        path = self.root / f"{name}.md"
        if not path.is_file():
            raise FileNotFoundError(f"required prompt asset not found: {path}")
        raw = path.read_text(encoding="utf-8")
        metadata, body = _parse_prompt(raw, path)
        if metadata.get("name") != name:
            raise ValueError(f"prompt name mismatch in {path}")
        version = metadata.get("version", "")
        if not re.fullmatch(r"v[1-9][0-9]*", version):
            raise ValueError(f"invalid prompt version in {path}: {version!r}")
        normalized = body.strip()
        return PromptAsset(
            name=name,
            version=version,
            description=metadata.get("description", ""),
            body=normalized,
            sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )

    def render(self, name: str, **values: object) -> str:
        asset = self.get_required(name)
        required = set(_PLACEHOLDER.findall(asset.body))
        missing = required - values.keys()
        unexpected = values.keys() - required
        if missing:
            raise ValueError(f"missing prompt values for {name}: {sorted(missing)}")
        if unexpected:
            raise ValueError(f"unexpected prompt values for {name}: {sorted(unexpected)}")
        rendered = asset.body
        for key, value in values.items():
            rendered = rendered.replace("{{" + key + "}}", str(value))
        if _PLACEHOLDER.search(rendered):
            raise ValueError(f"unresolved prompt placeholder in {name}")
        return f"PROMPT_ID={asset.prompt_id}\nPROMPT_SHA256={asset.sha256}\n{rendered}"

    def bundle_version(self, *names: str) -> str:
        assets = [self.get_required(name) for name in names]
        return "+".join(asset.prompt_id for asset in assets)

    def status_items(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for path in sorted(self.root.glob("*.md")):
            name = path.stem
            try:
                asset = self.get_required(name)
                items.append(
                    {
                        "name": asset.name,
                        "version": asset.version,
                        "promptId": asset.prompt_id,
                        "sha256": asset.sha256,
                        "status": "READY",
                    }
                )
            except (FileNotFoundError, ValueError) as exc:
                items.append({"name": name, "status": "FAILED", "reason": str(exc)})
        return items


@lru_cache(maxsize=1)
def prompt_catalog() -> PromptCatalog:
    return PromptCatalog()


def _parse_prompt(raw: str, path: Path) -> tuple[dict[str, str], str]:
    if not raw.startswith("---\n"):
        raise ValueError(f"prompt frontmatter is required: {path}")
    end = raw.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"prompt frontmatter is not closed: {path}")
    metadata: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ValueError(f"invalid prompt metadata line in {path}: {line!r}")
        metadata[key.strip()] = value.strip()
    return metadata, raw[end + 5 :]
