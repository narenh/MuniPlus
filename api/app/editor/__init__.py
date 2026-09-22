"""The station editor's server side (``/editor``, behind login) and the public map
(``/map``).

``install.install_editor`` wires all of it onto the app. The git write side
(``checkout``) builds on the read side in ``app.data.repo``; every write to an
sf-transit file goes through ``app.models.files``.
"""
