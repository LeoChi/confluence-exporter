"""Clean Confluence storage-format HTML so downstream renderers behave.

Confluence's storage format uses custom ``ac:*`` and ``ri:*`` namespaces for
macros, images, panels, tasks, etc. We strip / rewrite these into plain HTML
that every renderer (Playwright, WeasyPrint, xhtml2pdf, python-docx) can
handle consistently.

Pure function: input = HTML string + attachment map (``{filename: local_path}``),
output = clean HTML fragment string.
"""

from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def html_escape(s: str) -> str:
    return html.escape(s or "", quote=True)


def clean_confluence_html(html_str: str, attachment_map: dict[str, str]) -> str:
    """Return a clean HTML fragment (body contents only).

    * Replaces ``<ac:image>`` + ``<ri:attachment>`` with normal ``<img>``
      tags pointing to local files where possible.
    * Rewrites ``ac:task-list`` to ``<ul class="task-list">``.
    * Rewrites info/note/warning panels to styled divs.
    * Unwraps ``ac:structured-macro`` containers for common macros
      (``code``, ``noformat``, ``panel``, ``expand``).
    * Strips ``data-*`` and ``ac-*`` attributes (they confuse PDF renderers).
    * Wraps bare ``<tr>`` rows in ``<tbody>`` and adds a ``conf-table`` class.
    """
    soup = BeautifulSoup(html_str or "", "lxml")

    # -- images ------------------------------------------------------------
    for tag in soup.find_all(["ac:image", "ac-image"]):
        attachment = tag.find(["ri:attachment", "ri-attachment"])
        url_tag = tag.find(["ri:url", "ri-url"])
        new_img = soup.new_tag("img")
        if attachment and attachment.get("ri:filename"):
            filename = attachment.get("ri:filename")
            local = attachment_map.get(filename)
            if local:
                try:
                    new_img["src"] = Path(local).as_uri()
                except ValueError:
                    new_img["src"] = local
            else:
                new_img["src"] = filename
        elif url_tag and url_tag.get("ri:value"):
            new_img["src"] = url_tag.get("ri:value")
        else:
            tag.decompose()
            continue
        # Preserve alt text and dimensions where provided
        if tag.get("ac:alt"):
            new_img["alt"] = tag.get("ac:alt")
        if tag.get("ac:width"):
            new_img["style"] = f"max-width:{tag.get('ac:width')}px;"
        tag.replace_with(new_img)

    # -- task lists --------------------------------------------------------
    for tl in soup.find_all(["ac:task-list", "ac-task-list"]):
        ul = soup.new_tag("ul", attrs={"class": "task-list"})
        for task in tl.find_all(["ac:task", "ac-task"]):
            status = (task.find(["ac:task-status", "ac-task-status"]) or {}).get_text("")
            body_tag = task.find(["ac:task-body", "ac-task-body"])
            body_text = body_tag.decode_contents() if body_tag else ""
            li = soup.new_tag("li")
            mark = "[x] " if status.strip().lower() == "complete" else "[ ] "
            li.append(mark)
            li.append(BeautifulSoup(body_text, "lxml"))
            ul.append(li)
        tl.replace_with(ul)

    # -- macros (structured) ----------------------------------------------
    for macro in soup.find_all(["ac:structured-macro", "ac-structured-macro"]):
        name = (macro.get("ac:name") or macro.get("ac-name") or "").lower()
        body_tag = macro.find(
            ["ac:rich-text-body", "ac:plain-text-body",
             "ac-rich-text-body", "ac-plain-text-body"]
        )
        body_html = body_tag.decode_contents() if body_tag else ""

        if name in ("code", "noformat"):
            pre = soup.new_tag("pre")
            code = soup.new_tag("code")
            code.append(BeautifulSoup(body_html or "", "lxml"))
            pre.append(code)
            macro.replace_with(pre)
        elif name in ("info", "note", "warning", "tip"):
            color = {"info": "#DEEBFF", "tip": "#E3FCEF",
                     "note": "#FFFAE6", "warning": "#FFEBE6"}.get(name, "#F4F5F7")
            div = soup.new_tag(
                "div",
                attrs={
                    "class": f"conf-panel conf-panel-{name}",
                    "style": (
                        f"border-left:4px solid #0052CC;background:{color};"
                        "padding:8px 12px;margin:8px 0;"
                    ),
                },
            )
            div.append(BeautifulSoup(body_html or "", "lxml"))
            macro.replace_with(div)
        elif name in ("panel", "expand", "details"):
            div = soup.new_tag("div", attrs={"class": f"conf-{name}"})
            div.append(BeautifulSoup(body_html or "", "lxml"))
            macro.replace_with(div)
        else:
            # Unknown macro — unwrap to preserve content at least
            macro.unwrap()

    # -- link rewrites for internal attachment refs ------------------------
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        filename = a.get("data-linked-resource-default-alias") or a.get("data-filename")
        if filename and filename in attachment_map:
            try:
                a["href"] = Path(attachment_map[filename]).as_uri()
            except ValueError:
                a["href"] = attachment_map[filename]
        elif href and href.startswith("/wiki/"):
            # Leave internal wiki links as anchor-only; PDF readers will show them
            pass

    # -- tables: add <tbody> and style class ------------------------------
    for table in soup.find_all("table"):
        table["class"] = (table.get("class") or []) + ["conf-table"]
        rows = table.find_all("tr", recursive=False)
        if rows and not table.find("tbody"):
            tbody = soup.new_tag("tbody")
            for r in rows:
                tbody.append(r.extract())
            table.append(tbody)

    # -- strip noisy attributes -------------------------------------------
    _ATTR_PREFIXES = ("data-", "ac-", "ac:")
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if any(attr.startswith(p) for p in _ATTR_PREFIXES):
                del tag.attrs[attr]

    body = soup.body
    return body.decode_contents() if body else str(soup)


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except ValueError:
        return ""
