"""Web UI route handlers."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Custom Jinja2 filters
templates.env.filters["basename"] = lambda p: Path(str(p)).name
templates.env.filters["stem"] = lambda p: Path(str(p)).stem
