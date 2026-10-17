# Loaded automatically by gunicorn from the service's WorkingDirectory.
#
# gunicorn 25 starts a control-socket thread in the master before forking the
# worker. If the fork lands while that thread holds a lock, the worker hangs on a
# futex before the app loads: the service shows "active" but never answers.
# Beekeeper doesn't use the control socket, so turn it off.
control_socket_disable = True
