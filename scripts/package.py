import os
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
commit = os.environ.get("GITHUB_SHA", "development")
if commit != "development" and not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("GITHUB_SHA is invalid.")
# These are browser bundles. Node resolves the libraries through lib/client.js
# and lib/xml2js.js respectively (see their locked package.json entry points).
def node_only(parts):
    return parts[:3] != ("node_modules", "ali-oss", "dist") and parts != ("node_modules", "xml2js", "lib", "xml2js.bc.js")


dependency = ROOT / "node_modules" / "ali-oss" / "package.json"
if not dependency.is_file():
    raise SystemExit("Install production dependencies before packaging.")
target = ROOT / "dist" / "market-signal-review.zip"
target.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("index.js", "package.json"):
        archive.write(ROOT / name, name)
    archive.writestr("version.js", f'export const SOURCE_COMMIT = "{commit}";\n')
    for directory in ("src", "node_modules"):
        for path in sorted((ROOT / directory).rglob("*")):
            parts = path.relative_to(ROOT).parts
            if path.is_file() and node_only(parts) and not any(part.startswith(".") for part in parts):
                archive.write(path, "/".join(parts))
if target.stat().st_size > 20 * 1024 * 1024:
    raise SystemExit("ZIP exceeds the direct upload size limit.")
print(f"Packaged {target.name} ({target.stat().st_size} bytes) for {commit}")
