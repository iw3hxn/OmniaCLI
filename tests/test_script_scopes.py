"""A function declared inside an if-block of a page's script is hoisted to the
whole script (sloppy mode), over a function of the same name there: on
21/11/2026 the New staging dialog's show() replaced the instance page's tab
switch, and no tab of a production page opened."""
import re
from pathlib import Path

import pytest

TEMPLATES = sorted((Path(__file__).parent.parent / "omniacli" / "templates").glob("*.html"))


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_block_function_hides_one_of_the_script(template):
    text = template.read_text()
    top = set(re.findall(r"^  function (\w+)\(", text, re.M)) | set(re.findall(r"^function (\w+)\(", text, re.M))
    for block in re.finditer(r"^  if \([^\n]*\{\n(.*?)^  \}", text, re.M | re.S):
        inner = set(re.findall(r"^    function (\w+)\(", block.group(1), re.M))
        assert not inner & top, f"{template.name}: {sorted(inner & top)} declared in an if-block hides the script's own"
