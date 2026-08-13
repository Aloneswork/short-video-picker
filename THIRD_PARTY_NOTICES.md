# Third-party notices

The application's own source code is licensed under the MIT License; see `LICENSE`.

The application and its build process use the following direct third-party packages. Their own license terms continue to apply:

| Package | Validated version | License |
| --- | ---: | --- |
| Python | 3.13 | Python Software Foundation License |
| PyInstaller | 6.19.0 | GPL-2.0-or-later with the PyInstaller bootloader exception |
| Pillow | 12.0.0 | MIT-CMU |
| pywebview | 6.2.1 | BSD-3-Clause |
| Bottle | 0.13.4 | MIT |
| proxy-tools | 0.1.0 | BSD-2-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| websocket-client | 1.9.0 | Apache-2.0 |
| PyObjC and the listed framework bindings | 12.2.1 | MIT |

This is a direct-dependency notice, not a complete software bill of materials for every native library embedded by Python, Pillow, or the operating system. Public binary releases include this notice, the project's MIT license, and a `THIRD_PARTY_LICENSES` directory generated from the exact release environment. The public build rejects Conda/Anaconda runtimes and stops if a required license text cannot be collected.
