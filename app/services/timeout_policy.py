import os


def compute_size_scaled_timeout(
    *,
    path: str,
    base_timeout: int,
    timeout_per_gib: int,
    timeout_cap: int,
) -> int:
    """Compute a timeout as ``baseline + per-GiB allowance``, capped.

    The shared shape behind every size-sensitive bound in the app: a job that
    reads or writes a whole disc image takes time proportional to that image, so
    one flat number cannot serve a 400 MB CIA and a 90 GB PS3 ISO at once. Used
    for the conversion stall watchdog (:func:`compute_progress_stall_timeout`)
    and for the verify path's overall bound
    (``services.subprocess_runner.resolve_verify_timeout``).

    A baseline of 0 disables the bound entirely and returns 0, whatever the
    other knobs say. ``timeout_per_gib`` of 0 makes the bound flat, and a
    ``timeout_cap`` of 0 leaves it uncapped. An unreadable ``path`` is treated
    as 0 bytes: a bound that cannot be sized still applies at its baseline
    rather than vanishing.
    """
    baseline = max(0, int(base_timeout or 0))
    if baseline <= 0:
        return 0

    per_gib = max(0, int(timeout_per_gib or 0))
    cap = max(0, int(timeout_cap or 0))
    if per_gib <= 0:
        return min(baseline, cap) if cap > 0 else baseline

    try:
        size = max(0, int(os.path.getsize(path)))
    except OSError:
        size = 0

    gib = size / float(1024 ** 3)
    adaptive = baseline + int(gib * per_gib)
    timeout = max(baseline, adaptive)
    if cap > 0:
        timeout = min(timeout, cap)
    return timeout


def compute_progress_stall_timeout(
    *,
    input_path: str,
    base_timeout: int,
    timeout_per_gib: int,
    timeout_cap: int,
) -> int:
    """Compute an adaptive stall timeout from baseline + input size."""
    return compute_size_scaled_timeout(
        path=input_path,
        base_timeout=base_timeout,
        timeout_per_gib=timeout_per_gib,
        timeout_cap=timeout_cap,
    )
