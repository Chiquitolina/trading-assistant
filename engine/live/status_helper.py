def update_status(status_writer, **kwargs):
    state = status_writer._read_current() or {}
    state.update(kwargs)
    status_writer.write(state)