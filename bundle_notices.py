"""Copy installed runtime notices alongside the portable executable."""
from importlib import metadata
from pathlib import Path
import shutil
import sys

output_directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / 'dist'
destination = output_directory / 'licenses'
destination.mkdir(parents=True, exist_ok=True)
for package in ('numpy', 'soundfile', 'cffi', 'pyinstaller', 'psutil'):
    try:
        dist = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        continue  # Optional modules may not be installed in a clean build environment.
    for entry in dist.files:
        if entry.name.lower().startswith(('license', 'copying')):
            target = destination / package / Path(str(entry))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dist.locate_file(entry), target)
for source, name in [
    (Path(sys.base_prefix) / 'LICENSE.txt', 'Python-LICENSE.txt'),
    (Path(sys.base_prefix) / 'tcl/tcl8.6/license.terms', 'Tcl-license.terms'),
    (Path(sys.base_prefix) / 'tcl/tk8.6/license.terms', 'Tk-license.terms'),
    (Path(__file__).resolve().parent / 'reference/LICENSE', 'DAWproject-LICENSE'),
]:
    if source.is_file():
        shutil.copyfile(source, destination / name)
