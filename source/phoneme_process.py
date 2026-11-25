"""Run a local phoneme decoder and honor its optional inference lease."""
import subprocess


_LEASE_BUSY = 75


class PhonemeAlignmentBusy(ValueError):
    """The shared inference runtime is in use by another operation."""


def run_tool(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('The local phoneme aligner could not complete.') from error
    if args[0] == 'flock' and result.returncode == _LEASE_BUSY:
        raise PhonemeAlignmentBusy('The shared inference lease is busy.')
    if result.returncode:
        details = result.stderr.strip().splitlines()[-1:] or ['No output from the aligner.']
        raise ValueError('Local phoneme alignment failed. ' + details[0][:240])
    return result


def inference_command(executable, arguments, lease, wait_seconds=0):
    # The shared lease lives outside the project on machines that use one.
    # flock owns the lease for the lifetime of the short-lived CPU process.
    if not lease:
        return [str(executable), *arguments]
    wait = ['-w', str(wait_seconds)] if wait_seconds else ['-n']
    return ['flock', *wait, '-E', str(_LEASE_BUSY), str(lease), str(executable), *arguments]
