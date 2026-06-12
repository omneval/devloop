"""Guards against drift between the Helm chart's required-field validation
for `.Values.projects[]` entries and `_REQUIRED_FIELDS` in projects.py.
"""

import re
from pathlib import Path

from devloop.projects import _REQUIRED_FIELDS

_HELPERS_PATH = (
    Path(__file__).parent.parent / "charts" / "devloop" / "templates" / "_helpers.tpl"
)


def _chart_required_fields() -> list[str]:
    text = _HELPERS_PATH.read_text()
    match = re.search(
        r'{{- define "devloop\.projects\.requiredFields" -}}\n(.*?)\n{{- end -}}',
        text,
        re.DOTALL,
    )
    assert match, (
        "devloop.projects.requiredFields define block not found in _helpers.tpl"
    )
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def test_chart_required_fields_match_projects_py():
    assert sorted(_chart_required_fields()) == sorted(_REQUIRED_FIELDS)
