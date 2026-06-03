"""Manifest modułu Analizator (nowy dashboard + silnik audytu przetargowego)."""
from core.module_loader import ModuleManifest

manifest = ModuleManifest(
    name="wsk_analizator",
    version="1.0.0",
    display_name="Analizator przetargów",
    description="Nowy dashboard + analizator dokumentacji przetargowej (silnik audytu z folderu Drive)",
    author="WSK",
    category="core",
    depends=[],
    api_prefix="",
    enabled=True,
)
