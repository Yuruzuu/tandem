"""Process and schema helpers for the installed Claude Code CLI."""
import copy
import os
import shutil
import signal
import subprocess

MODEL = 'claude-opus-5-5'
INSTALL_HINT = 'Install the official Claude Code CLI or place claude.exe on PATH.'
WINDOWS_ESSENTIALS = ('SYSTEMROOT', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA')


def resolve_claude(command, env):
    head, *args = command or ['claude']
    executable = head if os.path.isabs(head) and os.access(head, os.X_OK) else shutil.which(head, path=env.get('PATH'))
    return [executable, *args] if executable else None


def own_process_group():
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {'start_new_session': True}


def windows_environment(env):
    if os.name == 'nt':
        present = {key.upper() for key in env}
        for key, value in os.environ.items():
            if key.upper() in WINDOWS_ESSENTIALS and key.upper() not in present:
                env[key] = value
    return env


def kill_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def normalize_input_schema(schema):
    normalized = copy.deepcopy(schema)
    for key in ('oneOf', 'allOf', 'anyOf'):
        if key in normalized:
            del normalized[key]
            normalized.setdefault('type', 'object')
    if normalized.get('type') == 'object' and not isinstance(normalized.get('properties'), dict):
        normalized['properties'] = {}
    return normalized
