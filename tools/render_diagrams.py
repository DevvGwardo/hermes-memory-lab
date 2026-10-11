#!/usr/bin/env python3
"""Render checked-in Mermaid sources to PNG and SVG with a pinned local CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "node_modules/.bin/mmdc"


def main():
    if not CLI.exists():
        raise SystemExit("Run npm ci first. Mermaid CLI is pinned in package-lock.json.")
    chrome = os.environ.get("CHROME_BIN")
    if not chrome:
        for candidate in [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
        ]:
            if candidate and Path(candidate).is_file():
                chrome = candidate
                break
    output = ROOT / "docs/images"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memory-lab-mermaid-") as td:
        config = Path(td) / "puppeteer.json"
        config.write_text(json.dumps({"executablePath": chrome} if chrome else {}))
        mermaid_config = Path(td) / "mermaid.json"
        mermaid_config.write_text(json.dumps({
            "theme": "dark",
            "themeVariables": {
                "darkMode": True,
                "background": "#0b1021",
                "primaryColor": "#131e36",
                "primaryBorderColor": "#06b6d4",
                "primaryTextColor": "#e0f2fe",
                "lineColor": "#38bdf8",
                "actorBkg": "#131e36",
                "actorBorder": "#06b6d4",
                "actorTextColor": "#e0f2fe",
                "actorLineColor": "#38bdf8",
                "signalColor": "#38bdf8",
                "signalTextColor": "#e2e8f0",
                "noteBkgColor": "#1e293b",
                "noteBorderColor": "#00f5d4",
                "noteTextColor": "#e2e8f0",
                "sequenceNumberColor": "#0b1021",
            },
        }))
        for source in sorted((ROOT / "docs/diagrams").glob("*.mmd")):
            for extension in ("svg", "png"):
                subprocess.run([
                    str(CLI), "-i", str(source), "-o", str(output / f"{source.stem}.{extension}"),
                    "-p", str(config), "-c", str(mermaid_config),
                    "-b", "#0b1021", "-t", "dark",
                    "--size", "1800", "-s", "2", "--no-font-embed",
                ], cwd=ROOT, check=True, timeout=90)
    print("Rendered three Mermaid sources as PNG and SVG.")


if __name__ == "__main__":
    main()
