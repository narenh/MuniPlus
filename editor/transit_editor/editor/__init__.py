"""The station editor's server side (``/``, behind login) and the public map
(``/map``).

``install.install_editor`` wires all of it onto the app. The git write side
(``checkout``) builds on the read side in ``transit_editor.data.repo``; every write to a
data file goes through ``transit_editor.models.files``.
"""
