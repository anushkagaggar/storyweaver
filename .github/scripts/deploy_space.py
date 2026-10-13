"""Upload backend/ to the Hugging Face Space. Run from backend/ after requirements.txt is exported."""

import os
import re
import sys
from pathlib import Path

from huggingface_hub import HfApi


def main() -> int:
    space_id = os.environ.get("HF_SPACE_ID")
    token = os.environ.get("HF_TOKEN")
    if not space_id or not token:
        print("HF_SPACE_ID (repo variable) and HF_TOKEN (secret) must be set.", file=sys.stderr)
        return 1

    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    match = re.search(r"^gradio==([^\s;]+)", requirements, re.MULTILINE)
    if not match:
        print("gradio is missing from requirements.txt.", file=sys.stderr)
        return 1

    # The Space's sdk_version must equal the locked gradio version.
    readme = Path("README.md")
    readme.write_text(
        re.sub(r"^sdk_version:.*$", f"sdk_version: {match.group(1)}", readme.read_text(encoding="utf-8"),
               count=1, flags=re.MULTILINE),
        encoding="utf-8",
    )

    HfApi(token=token).upload_folder(
        folder_path=".",
        repo_id=space_id,
        repo_type="space",
        commit_message=os.environ.get("COMMIT_MESSAGE", "Deploy"),
        ignore_patterns=["tests/**", ".venv/**", "**/__pycache__/**", ".env", "Dockerfile", ".dockerignore",
                         "uv.lock", "pyproject.toml"],
    )
    print(f"Deployed gradio {match.group(1)} app to https://huggingface.co/spaces/{space_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
