import pytest
from models_library.api_schemas_webserver.projects_nodes_ui import MarkerUI
from pydantic_extra_types.color import Color


@pytest.mark.parametrize(
    "color_str,expected_color_str", [("#b7e28d", "#b7e28d"), ("Cyan", "#0ff")]
)
def test_marker_color_serialized_to_hex(color_str, expected_color_str):
    m = MarkerUI(color=Color(color_str))
    assert m.model_dump_json() == f'{{"color":"{expected_color_str}"}}'
