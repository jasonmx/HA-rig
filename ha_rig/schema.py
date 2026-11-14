"""deviceside's test-record schema, which picks the format a run writes its records in, and checks each one first.

A checkout with `docs/test-records.schema.json` (target.SCHEMA) takes JSON format 2: each rig record and `_facts.json`
as that schema says, the contract's one home, in deviceside. One without it takes markdown, as deviceside `main` did
before its records moved to JSON. A file the schema refuses is never written.

`jsonschema` is imported only for a checkout with the schema, whose dev group has it, so a run against an older
checkout needs none. The runner loads the schema before anything moves (`load`), so a missing `jsonschema` or an
unreadable schema stops the run then, not after its cases, when no record could be written.
"""

import json
from pathlib import Path
from typing import Any

from .target import SCHEMA

FORMAT = 2  # the format the rig writes: each test with the devices it reached


class InvalidRecord(ValueError):
    """A file the schema refuses, so it is not written: its path and the schema's complaint."""

    def __init__(self, path: Path, error: str):
        super().__init__(f"{path.name} doesn't hold to deviceside's {SCHEMA}, so it is not written: {error}")
        self.path, self.error = path, error


class RecordSchema:
    """The checkout's test-record schema, with its validator."""

    def __init__(self, path: Path, validator: Any, best_match: Any):
        self.path, self.validator, self._best_match = path, validator, best_match

    @classmethod
    def load(cls, repo: Path) -> RecordSchema | None:
        """`repo`'s schema, or None when it has none (markdown records). Raises SystemExit, naming the fix, when the
        schema is there but `jsonschema` isn't importable, or the schema can't be read or isn't one."""
        path = repo / SCHEMA
        if not path.is_file():
            return None
        try:
            import jsonschema  # noqa: PLC0415 - only a checkout with the schema needs it
            from jsonschema.exceptions import best_match  # noqa: PLC0415
        except ImportError:
            raise SystemExit(
                f"{repo} keeps its test records as JSON ({SCHEMA}), which the rig validates with jsonschema, and this "
                "interpreter has none: run `uv sync` in the checkout, whose dev group has it"
            ) from None
        try:
            schema = json.loads(path.read_text())
            validator = jsonschema.validators.validator_for(schema)
            validator.check_schema(schema)
        except (OSError, ValueError, jsonschema.SchemaError) as err:
            raise SystemExit(f"Could not load {path}, so no record could be written: {err}") from None
        return cls(path, validator(schema), best_match)

    def error(self, document: Any) -> str | None:
        """What the schema says is most wrong with `document`, and where (as deviceside's check_release says it);
        None for a valid one."""
        if (found := self._best_match(self.validator.iter_errors(document))) is None:
            return None
        return f"{found.message}, at {'/'.join(map(str, found.absolute_path)) or 'the top level'}"

    def write(self, path: Path, document: Any) -> None:
        """Write `document` to `path` as JSON, indent 2 with a trailing newline, once the schema takes it. Raises
        InvalidRecord, writing nothing, when it doesn't."""
        if error := self.error(document):
            raise InvalidRecord(path, error)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(document))


def dumps(document: Any) -> str:
    """A record file's text: indent 2, a trailing newline, non-ASCII kept as it is."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
