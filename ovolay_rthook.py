# PyInstaller runtime hook for ovolay.
#
# Workaround for a PyGObject 3.56.x bug that crashes frozen builds on GLib
# >= 2.78: the unix_* helpers moved to the GLibUnix typelib, so
# gi/overrides/GLib.py skips adding unix_signal_add_full to __all__ while a
# separate code path still registers it as deprecated. load_overrides then
# raises "unix_signal_add_full was set deprecated but wasn't added to __all__".
# Retrying once works because the failed call already consumed the deprecation
# list via pop().

def _patch_gi_load_overrides():
    import gi.overrides
    import gi.importer

    original = gi.overrides.load_overrides

    def load_overrides(introspection_module):
        try:
            return original(introspection_module)
        except AssertionError as exc:
            if "was set deprecated but wasn't added to __all__" not in str(exc):
                raise
            return original(introspection_module)

    gi.overrides.load_overrides = load_overrides
    gi.importer.load_overrides = load_overrides


_patch_gi_load_overrides()
del _patch_gi_load_overrides
