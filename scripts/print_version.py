import pathlib
import re

text = (pathlib.Path(__file__).resolve().parents[1] / "src" / "vocalchop" / "__init__.py").read_text()
v = re.search(r'__version__ = "(\d+)\.(\d+)', text)
print(f"{v.group(1)}.{v.group(2)}")
