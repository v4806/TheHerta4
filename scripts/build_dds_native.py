"""Build the optional, Python-ABI-independent Windows x64 BC7 accelerator.

Usage: python scripts/build_dds_native.py --vcvars ".../VC/Auxiliary/Build/vcvars64.bat"
Generated headers/objects/import libraries stay in the system temp directory.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vcvars', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt' or not args.vcvars.is_file():
        parser.error('Windows and a valid vcvars64.bat are required')
    root = Path(__file__).resolve().parents[1]
    source = root / 'common/native/dds_decode.c'
    output = source.with_name('herta_dds_windows_x64.dll')
    spec = importlib.util.spec_from_file_location('dds_build_tables', root / 'common/dds_pixel.py')
    dds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dds)
    tables = [('P2', dds._BPTC_P2), ('P3', dds._BPTC_P3), ('A2', dds._BPTC_A2),
              ('A3', dds._BPTC_A3), ('FACTORS', dds._BPTC_FACTORS),
              ('MODES', __import__('numpy').asarray(dds._BC7_MODE_INFO))]
    work = Path(tempfile.mkdtemp(prefix='herta_dds_build_'))
    header = []
    for name, values in tables:
        ctype = 'uint32_t' if name in ('P2', 'P3') else 'unsigned char'
        dims = ''.join(f'[{n}]' for n in values.shape)
        initializer = str(values.tolist()).replace('[', '{').replace(']', '}')
        header.append(f'static const {ctype} {name}{dims} = {initializer};')
    (work / 'dds_tables.h').write_text('\n'.join(header), encoding='ascii')
    command = (f'@echo off\ncall "{args.vcvars}" >nul\nif errorlevel 1 exit /b 1\n'
               f'cl /nologo /O2 /LD /W4 /Zl /GS- /DHERTA_FREESTANDING_WINDOWS /I"{work}" "{source}" '
               f'/Fo"{work / "dds_decode.obj"}" /Fe"{output}" '
               f'/link /NODEFAULTLIB /NOENTRY /IMPLIB:"{work / "dds_decode.lib"}"\nexit /b %errorlevel%\n')
    script = work / 'build.cmd'
    script.write_text(command, encoding='utf-8')
    subprocess.run(['cmd.exe', '/d', '/c', str(script)], cwd=work, check=True)
    print(f'Built {output}')
    print(f'Build files: {work}')


if __name__ == '__main__':
    main()
