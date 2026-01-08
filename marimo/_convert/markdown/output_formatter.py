# Copyright 2026 Marimo. All rights reserved.
"""Format cell outputs for markdown export."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from marimo import _loggers

if TYPE_CHECKING:
    from marimo._server.export import ExternalFile

LOGGER = _loggers.marimo_logger()


@dataclass
class FormattedOutput:
    """Result of formatting a cell output."""

    markdown: str
    external_files: list["ExternalFile"]


def format_cell_outputs(
    outputs: list[dict[str, Any]],
    console: list[dict[str, Any]],
    cell_index: int,
    code_hash: str,
    output_dir: Optional[Path] = None,
    notebook_name: str = "notebook",
) -> FormattedOutput:
    """Format all outputs for a cell into markdown.

    Args:
        outputs: List of DataOutput or ErrorOutput dictionaries.
        console: List of StreamOutput or StreamMediaOutput dictionaries.
        cell_index: Index of the cell in the notebook.
        code_hash: Hash of the cell's code (for unique filenames).
        output_dir: Directory for saving external files. If None, images are
            embedded as base64 data URLs.
        notebook_name: Name of the notebook (for filenames).

    Returns:
        FormattedOutput containing markdown text and external files.
    """
    from marimo._server.export import ExternalFile

    markdown_parts: list[str] = []
    external_files: list[ExternalFile] = []

    # Format console output (stdout/stderr) first
    console_md = _format_console(console)
    if console_md:
        markdown_parts.append(console_md)

    # Format main outputs
    for i, output in enumerate(outputs):
        output_type = output.get("type")

        if output_type == "error":
            md = _format_error_output(output)
            if md:
                markdown_parts.append(md)

        elif output_type == "data":
            data = output.get("data", {})
            md, files = _format_data_output(
                data=data,
                cell_index=cell_index,
                output_index=i,
                code_hash=code_hash,
                output_dir=output_dir,
                notebook_name=notebook_name,
            )
            if md:
                markdown_parts.append(md)
            external_files.extend(files)

    if not markdown_parts:
        return FormattedOutput(markdown="", external_files=[])

    # Wrap outputs in a details element
    output_content = "\n\n".join(markdown_parts)
    markdown = f"""<details open>
<summary>Output</summary>

{output_content}

</details>"""

    return FormattedOutput(markdown=markdown, external_files=external_files)


def _format_console(console: list[dict[str, Any]]) -> str:
    """Format console outputs (stdout/stderr) as markdown."""
    if not console:
        return ""

    parts: list[str] = []
    current_stream: Optional[str] = None
    current_text: list[str] = []

    for item in console:
        item_type = item.get("type")

        if item_type == "stream":
            name = item.get("name")  # "stdout" or "stderr"
            text = item.get("text", "")

            if name != current_stream and current_text:
                # Flush previous stream
                parts.append(_format_stream_block(current_stream, current_text))
                current_text = []

            current_stream = name
            current_text.append(text)

        elif item_type == "streamMedia":
            # Media in console stream - typically images
            if current_text:
                parts.append(_format_stream_block(current_stream, current_text))
                current_text = []
                current_stream = None

            # Handle media output
            mimetype = item.get("mimetype", "")
            data = item.get("data", "")
            if mimetype.startswith("image/") and data:
                parts.append(f"![console output]({data})")

    # Flush remaining text
    if current_text:
        parts.append(_format_stream_block(current_stream, current_text))

    return "\n\n".join(parts)


def _format_stream_block(
    stream_name: Optional[str], text_parts: list[str]
) -> str:
    """Format a stream (stdout/stderr) as a code block."""
    text = "".join(text_parts).rstrip()
    if not text:
        return ""

    if stream_name == "stderr":
        return f"```stderr\n{text}\n```"
    else:
        return f"```\n{text}\n```"


def _format_error_output(error: dict[str, Any]) -> str:
    """Format an error output as markdown."""
    ename = error.get("ename", "Error")
    evalue = error.get("evalue", "")
    traceback = error.get("traceback", [])

    parts: list[str] = []

    # Error header
    if evalue:
        parts.append(f"**{ename}**: {evalue}")
    else:
        parts.append(f"**{ename}**")

    # Traceback
    if traceback:
        tb_text = "\n".join(traceback)
        # Remove ANSI escape codes
        tb_text = re.sub(r"\x1b\[[0-9;]*m", "", tb_text)
        parts.append(f"```\n{tb_text}\n```")

    return "\n\n".join(parts)


def _format_data_output(
    data: dict[str, Any],
    cell_index: int,
    output_index: int,
    code_hash: str,
    output_dir: Optional[Path],
    notebook_name: str,
) -> tuple[str, list["ExternalFile"]]:
    """Format a data output (MIME bundle) as markdown.

    Returns:
        Tuple of (markdown_text, list_of_external_files).
    """
    from marimo._server.export import ExternalFile

    external_files: list[ExternalFile] = []

    # Priority order for MIME types
    # 1. Images (prefer SVG for vector graphics)
    # 2. HTML
    # 3. Markdown
    # 4. Plain text

    # Check for images first
    image_mimes = [
        ("image/svg+xml", "svg"),
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/gif", "gif"),
    ]

    for mime, ext in image_mimes:
        if mime in data:
            image_data = data[mime]
            md, files = _format_image(
                image_data=image_data,
                mime_type=mime,
                extension=ext,
                cell_index=cell_index,
                output_index=output_index,
                code_hash=code_hash,
                output_dir=output_dir,
                notebook_name=notebook_name,
            )
            return md, files

    # Check for Vega/Vega-Lite (convert to image if possible)
    vega_mimes = [
        "application/vnd.vegalite.v5+json",
        "application/vnd.vegalite.v4+json",
        "application/vnd.vega.v5+json",
        "application/vnd.vega.v4+json",
    ]
    for mime in vega_mimes:
        if mime in data:
            vega_spec = data[mime]
            md, files = _format_vega(
                vega_spec=vega_spec,
                cell_index=cell_index,
                output_index=output_index,
                code_hash=code_hash,
                output_dir=output_dir,
                notebook_name=notebook_name,
            )
            if md:
                return md, files

    # Check for HTML
    if "text/html" in data:
        html = data["text/html"]
        # For now, include HTML as-is (may not render on GitHub)
        # Could potentially convert to markdown in the future
        return html, []

    # Check for markdown
    if "text/markdown" in data:
        return data["text/markdown"], []

    # Fall back to plain text
    if "text/plain" in data:
        text = data["text/plain"]
        if text.strip():
            return f"```\n{text}\n```", []

    return "", []


def _format_image(
    image_data: str,
    mime_type: str,
    extension: str,
    cell_index: int,
    output_index: int,
    code_hash: str,
    output_dir: Optional[Path],
    notebook_name: str,
) -> tuple[str, list["ExternalFile"]]:
    """Format an image output.

    Args:
        image_data: The image data (base64 or data URL).
        mime_type: The MIME type of the image.
        extension: File extension for the image.
        cell_index: Index of the cell.
        output_index: Index of the output within the cell.
        code_hash: Hash of the cell's code.
        output_dir: Directory for saving external files.
        notebook_name: Name of the notebook.

    Returns:
        Tuple of (markdown_text, list_of_external_files).
    """
    from marimo._server.export import ExternalFile

    # Extract base64 data from data URL if necessary
    if image_data.startswith("data:"):
        # Parse data URL: data:[<mediatype>][;base64],<data>
        match = re.match(r"data:[^;,]*(?:;base64)?,(.+)", image_data)
        if match:
            base64_data = match.group(1)
        else:
            base64_data = image_data
    else:
        base64_data = image_data

    # Decode the image
    try:
        image_bytes = base64.b64decode(base64_data)
    except Exception as e:
        LOGGER.warning(f"Failed to decode image: {e}")
        return "", []

    # Generate filename
    hash_prefix = code_hash[:8] if code_hash else "unknown"
    filename = f"{notebook_name}_cell_{cell_index}_{hash_prefix}"
    if output_index > 0:
        filename += f"_{output_index}"
    filename += f".{extension}"

    if output_dir:
        # Save as external file
        relative_path = f"images/{filename}"
        external_file = ExternalFile(
            relative_path=relative_path,
            content=image_bytes,
        )
        markdown = f"![Output](./{relative_path})"
        return markdown, [external_file]
    else:
        # Embed as data URL
        data_url = f"data:{mime_type};base64,{base64_data}"
        markdown = f"![Output]({data_url})"
        return markdown, []


def _format_vega(
    vega_spec: Any,
    cell_index: int,
    output_index: int,
    code_hash: str,
    output_dir: Optional[Path],
    notebook_name: str,
) -> tuple[str, list["ExternalFile"]]:
    """Format a Vega/Vega-Lite spec, converting to image if possible.

    Returns:
        Tuple of (markdown_text, list_of_external_files).
    """
    try:
        import vl_convert as vlc
    except ImportError:
        LOGGER.debug(
            "vl_convert not installed, embedding Vega spec as JSON code block"
        )
        # Fall back to showing the spec as JSON
        import json

        spec_json = json.dumps(vega_spec, indent=2)
        return f"```json\n{spec_json}\n```", []

    try:
        # Convert to PNG
        png_data = vlc.vegalite_to_png(vega_spec)

        # Generate filename
        hash_prefix = code_hash[:8] if code_hash else "unknown"
        filename = f"{notebook_name}_cell_{cell_index}_{hash_prefix}"
        if output_index > 0:
            filename += f"_{output_index}"
        filename += ".png"

        from marimo._server.export import ExternalFile

        if output_dir:
            relative_path = f"images/{filename}"
            external_file = ExternalFile(
                relative_path=relative_path,
                content=png_data,
            )
            markdown = f"![Output](./{relative_path})"
            return markdown, [external_file]
        else:
            # Embed as data URL
            base64_data = base64.b64encode(png_data).decode("utf-8")
            data_url = f"data:image/png;base64,{base64_data}"
            markdown = f"![Output]({data_url})"
            return markdown, []

    except Exception as e:
        LOGGER.warning(f"Failed to convert Vega spec to image: {e}")
        # Fall back to showing the spec as JSON
        import json

        spec_json = json.dumps(vega_spec, indent=2)
        return f"```json\n{spec_json}\n```", []
