from pathlib import Path

# Keep package on disk next to the .pyd - do not archive __init__.py into PYZ.
module_collection_mode = {
    "fast_walk": "py",
}

hiddenimports = ["fast_walk", "fast_walk.fast_walk"]

binaries = []
datas = []

try:
    import fast_walk as module

    package_dir = Path(module.__file__).resolve().parent
    for artifact in sorted(package_dir.glob("fast_walk*.pyd")):
        binaries.append((str(artifact), "fast_walk"))
    init_py = package_dir / "__init__.py"
    if init_py.is_file():
        datas.append((str(init_py), "fast_walk"))
except ImportError:
    dll = Path("fast_walk/target/release/fast_walk.dll")
    if dll.is_file():
        binaries.append((str(dll.resolve()), "fast_walk"))