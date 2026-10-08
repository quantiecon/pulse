import importlib.util
from pathlib import Path

from fastapi import FastAPI


def test_vercel_entrypoint_exports_the_pulse_app():
    path = Path(__file__).resolve().parents[1] / "src" / "app.py"
    spec = importlib.util.spec_from_file_location("pulse_vercel_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert isinstance(module.app, FastAPI)
    assert module.app.title == "Pulse"
