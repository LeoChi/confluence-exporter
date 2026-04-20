# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added
- Interactive Rich-powered TUI with banners, colored prompts, progress bars, and summary tables.
- Three subcommands: `export`, `convert`, `merge` — each also reachable from an interactive menu.
- **Export**: download an entire Confluence Cloud space (pages + attachments + Gliffy diagrams) as HTML, Markdown, DOCX, or PDF.
- **Convert**: turn a tree of exported HTML files into clean PDFs/DOCX, with inline attachment embedding and automatic PDF-attachment merging.
- **Merge**: consolidate many per-page PDFs into volumes with a generated Table of Contents and a hierarchical PDF bookmark outline — NotebookLM-ready.
- Three authentication strategies: API token (Basic), Personal Access Token (Bearer), and **generic browser-cookie paste** that accepts any session cookie name (`cloud.session.token`, `tenant.session.token`, or a whole `Cookie:` header copied from DevTools).
- Pluggable PDF engine system with automatic fallback: Playwright → WeasyPrint → xhtml2pdf.
- Windows long-path (`\\?\`) handling across every file I/O.
- Per-space lockfile for resumable, incremental exports.

[0.1.0]: https://github.com/LeoChi/confluence-exporter/releases/tag/v0.1.0
