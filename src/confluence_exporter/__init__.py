"""Confluence Exporter — export a Confluence space to PDF/DOCX/MD/HTML.

Public API (for library use):

    from confluence_exporter import ConfluenceClient, SpaceExporter, \\
        OutputConverter, PDFMerger, load_config

See the README or run ``confluence-exporter --help`` for CLI usage.
"""

from confluence_exporter.client import ConfluenceClient
from confluence_exporter.config import AppConfig, load_config, save_config
from confluence_exporter.converter import OutputConverter
from confluence_exporter.exporter import SpaceExporter
from confluence_exporter.merger import PDFMerger

__version__ = "0.1.0"

__all__ = [
    "AppConfig",
    "ConfluenceClient",
    "OutputConverter",
    "PDFMerger",
    "SpaceExporter",
    "__version__",
    "load_config",
    "save_config",
]
