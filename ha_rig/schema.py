"""deviceside's test-record schema, which picks the format a run writes its records in, and checks each one first.

A checkout with `docs/test-records.schema.json` (target.SCHEMA) takes JSON: each rig record and `_facts.json` as that
schema says, the contract's one home, in deviceside. One without it takes markdown, as deviceside `main` did before its
records moved to JSON. A file the schema refuses is never written.

A record is format 3 (bound to the action's call transcript, `calls`) when the schema's record format enum lists 3
(`RecordSchema.binds_calls`), else format 2 (bound to the package tree), so a checkout from before format 3 still gets
what it validates. `_facts.json` is format 2 whatever the record's, as the schema pins it.

`jsonschema` is imported only for a checkout with the schema, whose dev group has it, so a run against an older
checkout needs none. The runner loads the schema before anything moves (`load`), so a missing `jsonschema` or an
unreadable schema stops the run then, not after its cases, when no record could be written.
"""

import json
import sys
from pathlib import Path
from typing import Any

from .target import SCHEMA

FORMAT = 2  # `_facts.json`'s format, and a record bound to the package tree: each test with the devices it reached
CALLS_FORMAT = 3  # a record bound to the action's call transcript instead of the package tree


class InvalidRecord(ValueError):
    """A file the schema refuses, so it is not written: its path and the schema's complaint."""

    def __init__(self, path: Path, error: str):
        super().__init__(f"{path.name} doesn't hold to deviceside's {SCHEMA}, so it is not written: {error}")
        self.path, self.error = path, error


class RecordSchema:
    """The checkout's test-record schema, with its validator."""

    def __init__(self, path: Path, validator: Any, best_match: Any, binds_calls: bool = False):
        self.path, self.validator, self._best_match = path, validator, best_match
        self.binds_calls = binds_calls  # the schema's records may be format 3 (`calls`), not only the package tree's 2

    @classmethod
    def load(cls, repo: Path) -> RecordSchema | None:
        """`repo`'s schema, or None when it has none (markdown records). Raises SystemExit, naming the fix, when the
        schema is there but `jsonschema` isn't importable, or the schema can't be read or isn't one."""
        path = repo / SCHEMA
        if not path.is_file():
            return None
        try:
            import jsonschema  # noqa: PLC0415 - only a checkout with the schema needs it
        except ImportError:
            raise SystemExit(
                f"{repo} keeps its test records as JSON ({SCHEMA}), which the rig validates with jsonschema, and "
                f"{sys.executable} has none: `uv sync` the project it runs in (the deviceside checkout's, which the "
                "shebang's `uv run` picks when run from there, and whose dev group has it)"
            ) from None
        try:
            return cls.parse(path, path.read_text())
        except (OSError, ValueError, jsonschema.SchemaError) as err:
            raise SystemExit(f"Could not load {path}, so no record could be written: {err}") from None

    @classmethod
    def parse(cls, path: Path, text: str) -> RecordSchema:
        """The schema `text` holds (`path` names it). Raises ValueError or jsonschema's SchemaError for one that isn't
        a schema."""
        import jsonschema  # noqa: PLC0415 - only a checkout with the schema needs it
        from jsonschema.exceptions import best_match  # noqa: PLC0415

        schema = json.loads(text)
        validator = jsonschema.validators.validator_for(schema)
        validator.check_schema(schema)
        return cls(path, validator(schema), best_match, CALLS_FORMAT in _record_formats(schema))

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


def _record_formats(schema: Any) -> list:
    """The formats the schema's rig record allows (its `record` definition's `format` enum); none for a schema not
    shaped so, which then takes format 2 as before."""
    try:
        formats = schema["$defs"]["record"]["properties"]["format"]["enum"]
    except KeyError, TypeError:
        return []
    return formats if isinstance(formats, list) else []


def dumps(document: Any) -> str:
    """A record file's text: indent 2, a trailing newline, non-ASCII kept as it is."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
