# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={
        "gi": {
            "themes": ["Adwaita"],
            "icons": ["Adwaita"],
            "languages": ["en_US"],
            "module-versions": {
                "Gtk": "4.0",
                "Gtk4LayerShell": "1.0",
                "Gdk": "4.0",
                "GdkPixbuf": "2.0",
                "Pango": "1.0",
                "Gio": "2.0",
                "GLib": "2.0",
                "GObject": "2.0",
            },
        },
    },
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],  # Leave this empty to prevent one-file bundling 
    exclude_binaries=True, # Essential for one-dir mode
    name='volume-overlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,  # Strip symbols to make binaries smaller and faster to load
    upx=False,   # DISABLE UPX: This is the biggest speed win after one-dir mode
    console=False, # Prevents the delay/flicker of a terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=True,
    upx=False,
    name='volume-overlay',
)
