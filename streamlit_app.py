"""Root entrypoint for Streamlit Community Cloud.

Cloud defaults to looking for an app file at the repository root, and its
main-file-path validation is fussy about nested paths. This shim keeps the
real app where it belongs and lets the deploy form use its default value.

Deploy with:  Main file path = streamlit_app.py
"""

import runpy
from pathlib import Path

APP = Path(__file__).parent / "healthcare-claims-analytics" / "app" / "app.py"

runpy.run_path(str(APP), run_name="__main__")
