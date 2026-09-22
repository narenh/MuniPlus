"""Shared model configuration.

Everything on disk and on the wire is camelCase, matching the app (Swift) and the
data files that came before. Python attributes stay snake_case.

File models forbid unknown keys, because they are hand-edited and a misspelt key
(``transferAgency``) must fail loudly rather than be silently dropped on the next
save. Response models do not need that and use ``Wire`` instead.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class FileModel(Wire):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="forbid",
    )
